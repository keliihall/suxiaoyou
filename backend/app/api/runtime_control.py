"""Local-only v1.1 checkpoint rewind and managed-worktree control plane.

The HTTP boundary accepts durable database identities only.  Workspace and
repository paths are always resolved from ``Session``/``Project``/
``WorkspaceInstance`` rows and are never part of a request schema.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.local import require_local_session
from app.dependencies import SessionFactoryDep, StreamManagerDep
from app.models.project import Project
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.workspace_instance import WorkspaceInstance
from app.release_readiness import v11_capability_released
from app.runtime.rewind import (
    RewindBusyError,
    RewindConflictError,
    RewindDisabledError,
    RewindError,
    RewindNotFoundError,
    RewindProvenanceError,
    RewindService,
    rewind_runtime_enabled,
)
from app.security.audit import AuditPersistenceError, record_security_event
from app.storage.checkpoints import (
    CheckpointConflictError,
    CheckpointNotFoundError,
    inspect_workspace_identity,
)
from app.validation_agent.persistence import (
    PublicCheckpointValidationSummary,
    invalid_validation_summary,
    not_requested_validation_summary,
    parse_public_checkpoint_validation_summary,
)
from app.worktree import (
    GitCommandError,
    GitCommandTimeout,
    GitUnavailableError,
    RepositoryValidationError,
    WorktreeActiveError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreeError,
    WorktreeFeatureDisabled,
    WorktreeNotFoundError,
    WorktreeOwnershipError,
    WorktreePathError,
    WorktreeRuntime,
    WorktreeService,
    worktree_runtime_enabled,
)


router = APIRouter(
    prefix="/runtime",
    dependencies=[Depends(require_local_session)],
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RewindRequest(_StrictBody):
    session_id: str = Field(min_length=1, max_length=128)
    workspace_instance_id: str = Field(min_length=1, max_length=128)
    checkpoint_id: str = Field(min_length=1, max_length=128)


class WorktreeCreateRequest(_StrictBody):
    session_id: str = Field(min_length=1, max_length=128)
    ref: str = Field(default="HEAD", min_length=1, max_length=1024)
    branch: str | None = Field(default=None, min_length=1, max_length=255)


class WorktreeInstanceRequest(_StrictBody):
    session_id: str = Field(min_length=1, max_length=128)
    workspace_instance_id: str = Field(min_length=1, max_length=128)


def _error(
    *,
    status_code: int,
    code: str,
    detail: str,
    **extra: Any,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, **extra},
    )


def _rewind_unavailable() -> JSONResponse:
    return _error(
        status_code=404,
        code="v11_rewind_not_available",
        detail="Checkpoint rewind is not available in this release",
    )


def _worktree_unavailable() -> JSONResponse:
    return _error(
        status_code=404,
        code="v11_worktree_not_available",
        detail="Managed worktrees are not available in this release",
    )


def _runtime_unavailable() -> JSONResponse:
    return _error(
        status_code=404,
        code="v11_runtime_not_available",
        detail="The v1.1 local runtime control plane is not available",
    )


def _safe_external_effects(
    effects: Iterable[dict[str, Any]],
) -> list[dict[str, str]]:
    """Expose only the redacted checkpoint ledger vocabulary."""

    public: list[dict[str, str]] = []
    for effect in effects:
        item: dict[str, str] = {}
        for key in ("checkpoint_id", "source", "operation", "audit_id"):
            value = effect.get(key)
            if isinstance(value, str) and value:
                item[key] = value[:200]
        if item:
            public.append(item)
    return public


async def _checkpoint_validation_summaries(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    workspace_instance_id: str,
    checkpoint_ids: Iterable[str],
) -> dict[str, PublicCheckpointValidationSummary]:
    """Load public validator summaries in one bounded database query."""

    unique_ids = tuple(dict.fromkeys(checkpoint_ids))
    if not unique_ids:
        return {}
    if not v11_capability_released("validator"):
        return {
            checkpoint_id: not_requested_validation_summary()
            for checkpoint_id in unique_ids
        }

    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(SessionCheckpoint).where(
                        SessionCheckpoint.id.in_(unique_ids),
                        SessionCheckpoint.session_id == session_id,
                        SessionCheckpoint.workspace_instance_id
                        == workspace_instance_id,
                    )
                )
            ).scalars()
        )
    by_id = {checkpoint.id: checkpoint for checkpoint in rows}
    return {
        checkpoint_id: (
            parse_public_checkpoint_validation_summary(by_id[checkpoint_id])
            if checkpoint_id in by_id
            else invalid_validation_summary()
        )
        for checkpoint_id in unique_ids
    }


def _rewind_error(exc: RewindError) -> tuple[str, JSONResponse]:
    if isinstance(exc, RewindDisabledError):
        return "v11_rewind_not_available", _rewind_unavailable()
    if isinstance(exc, RewindNotFoundError):
        return "rewind_not_found", _error(
            status_code=404,
            code="rewind_not_found",
            detail="Checkpoint rewind resource was not found",
        )
    if isinstance(exc, RewindProvenanceError):
        return "rewind_provenance_mismatch", _error(
            status_code=409,
            code="rewind_provenance_mismatch",
            detail="The checkpoint does not belong to the requested session workspace",
        )
    if isinstance(exc, RewindBusyError):
        return "rewind_busy", _error(
            status_code=409,
            code="rewind_busy",
            detail=str(exc),
        )
    if isinstance(exc, RewindConflictError):
        conflicts = [
            {
                "relative_path": conflict.relative_path,
                "reason": conflict.reason,
            }
            for conflict in exc.conflicts
        ]
        return "rewind_conflict", _error(
            status_code=409,
            code="rewind_conflict",
            detail=str(exc),
            conflicts=conflicts,
        )
    return "rewind_failed", _error(
        status_code=409,
        code="rewind_failed",
        detail="Checkpoint rewind failed safely",
    )


def _worktree_error(exc: Exception) -> tuple[str, JSONResponse]:
    if isinstance(exc, WorktreeFeatureDisabled):
        return "v11_worktree_not_available", _worktree_unavailable()
    if isinstance(exc, (CheckpointNotFoundError, WorktreeNotFoundError)):
        return "worktree_not_found", _error(
            status_code=404,
            code="worktree_not_found",
            detail="Managed worktree resource was not found",
        )
    if isinstance(exc, WorktreeDirtyError):
        return "worktree_dirty", _error(
            status_code=409,
            code="worktree_dirty",
            detail="The Git workspace has uncommitted changes",
        )
    if isinstance(exc, WorktreeActiveError):
        return "worktree_active", _error(
            status_code=409,
            code="worktree_active",
            detail=str(exc),
        )
    if isinstance(exc, WorktreeOwnershipError):
        return "worktree_ownership_mismatch", _error(
            status_code=409,
            code="worktree_ownership_mismatch",
            detail="Managed worktree ownership could not be verified",
        )
    if isinstance(exc, RepositoryValidationError):
        return "worktree_repository_invalid", _error(
            status_code=409,
            code="worktree_repository_invalid",
            detail="The session repository is not eligible for a managed worktree",
        )
    if isinstance(exc, WorktreePathError):
        return "worktree_path_invalid", _error(
            status_code=409,
            code="worktree_path_invalid",
            detail="Managed worktree storage failed its safety checks",
        )
    if isinstance(exc, (CheckpointConflictError, WorktreeConflictError)):
        return "worktree_conflict", _error(
            status_code=409,
            code="worktree_conflict",
            detail="Managed worktree state conflicts with the requested operation",
        )
    if isinstance(exc, GitUnavailableError):
        return "git_unavailable", _error(
            status_code=503,
            code="git_unavailable",
            detail="Git is unavailable",
        )
    if isinstance(exc, GitCommandTimeout):
        return "git_timeout", _error(
            status_code=504,
            code="git_timeout",
            detail="The supervised Git operation timed out",
        )
    if isinstance(exc, GitCommandError):
        return "git_failed", _error(
            status_code=502,
            code="git_failed",
            detail="The supervised Git operation failed",
        )
    if isinstance(exc, WorktreeError):
        return "worktree_failed", _error(
            status_code=409,
            code="worktree_failed",
            detail="The managed worktree operation failed safely",
        )
    raise exc


async def _record_mutation_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    capability: str,
    action: str,
    outcome: str,
    session_id: str,
    workspace_instance_id: str | None = None,
    checkpoint_id: str | None = None,
    machine_code: str | None = None,
    required: bool = False,
) -> None:
    details: dict[str, Any] = {}
    if workspace_instance_id is not None:
        details["workspace_instance_id"] = workspace_instance_id
    if checkpoint_id is not None:
        details["checkpoint_id"] = checkpoint_id
    if machine_code is not None:
        details["machine_code"] = machine_code
    await record_security_event(
        session_factory,
        source_kind="runtime_control",
        source_id="desktop",
        invocation_source_kind="desktop",
        capability=capability,
        action=action,
        decision="system",
        outcome=outcome,
        session_id=session_id,
        details=details,
        required=required,
    )


def _rewind_service(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession],
    stream_manager: Any,
) -> RewindService:
    override = getattr(request.app.state, "v11_rewind_service", None)
    if override is not None:
        return override
    return RewindService(
        session_factory,
        stream_manager=stream_manager,
    )


def _worktree_runtime(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession],
    stream_manager: Any,
) -> WorktreeRuntime:
    existing = getattr(request.app.state, "v11_worktree_runtime", None)
    if existing is not None:
        return existing
    service = WorktreeService(enabled=worktree_runtime_enabled())
    runtime = WorktreeRuntime(
        session_factory,
        service,
        stream_manager=stream_manager,
    )
    request.app.state.v11_worktree_runtime = runtime
    return runtime


def _same_resolved_path(left: str, right: str) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=True) == Path(right).expanduser().resolve(
            strict=True
        )
    except (OSError, RuntimeError, ValueError):
        return False


async def _require_current_checkpoint_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    workspace_instance_id: str,
) -> None:
    async with session_factory() as db:
        session = await db.get(Session, session_id)
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
    if session is None or instance is None:
        raise RewindNotFoundError("Session or workspace instance not found")
    if (
        instance.status != "active"
        or not _same_resolved_path(session.directory, instance.root_path)
        or (
            session.project_id is not None
            and instance.project_id is not None
            and session.project_id != instance.project_id
        )
    ):
        raise RewindProvenanceError(
            "Workspace instance is not the session's current server-owned workspace"
        )
    try:
        canonical, identity = await asyncio.to_thread(
            inspect_workspace_identity, instance.root_path
        )
    except Exception as exc:
        raise RewindProvenanceError(
            "Workspace filesystem identity is unavailable"
        ) from exc
    if canonical != instance.root_path or identity != instance.identity_token:
        raise RewindProvenanceError("Workspace filesystem identity has changed")


async def _current_workspace_context(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
) -> tuple[Session, WorkspaceInstance]:
    async with session_factory() as db:
        session = await db.get(Session, session_id)
        if session is None:
            raise RewindNotFoundError("Session was not found")
        raw_root = session.directory
        if not raw_root or raw_root == ".":
            raise RewindProvenanceError("Session has no selected workspace")
        try:
            canonical, identity = await asyncio.to_thread(
                inspect_workspace_identity,
                raw_root,
            )
        except Exception as exc:
            raise RewindProvenanceError(
                "Workspace filesystem identity is unavailable"
            ) from exc
        candidates = list(
            (
                await db.execute(
                    select(WorkspaceInstance)
                    .where(
                        WorkspaceInstance.root_path == canonical,
                        WorkspaceInstance.identity_token == identity,
                        WorkspaceInstance.status == "active",
                    )
                    .order_by(WorkspaceInstance.time_created.desc())
                    .limit(8)
                )
            ).scalars()
        )
        instance = next(
            (
                item
                for item in candidates
                if not (
                    session.project_id is not None
                    and item.project_id is not None
                    and session.project_id != item.project_id
                )
            ),
            None,
        )
        if instance is None:
            raise RewindNotFoundError("Active workspace instance was not found")
        return session, instance


async def _session_repository(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> str:
    async with session_factory() as db:
        session = await db.get(Session, session_id)
        if session is None:
            raise CheckpointNotFoundError("Session not found")
        project = (
            await db.get(Project, session.project_id)
            if session.project_id is not None
            else None
        )
        if session.project_id is not None and project is None:
            raise WorktreeConflictError("Session project is unavailable")
        repository = project.worktree if project is not None else session.directory
        if project is not None and not _same_resolved_path(
            session.directory, project.worktree
        ):
            raise WorktreeConflictError(
                "Session is not bound to its database-owned project repository"
            )
    try:
        return str(Path(repository).expanduser().resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorktreeConflictError("Session repository is unavailable") from exc


async def _worktree_db_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    workspace_instance_id: str,
) -> tuple[str, dict[str, Any]]:
    async with session_factory() as db:
        session = await db.get(Session, session_id)
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
        if session is None or instance is None:
            raise CheckpointNotFoundError("Session or workspace instance not found")
        details = dict(instance.details or {})
        if (
            instance.kind != "git_worktree"
            or details.get("session_id") != session.id
            or instance.created_by_session_id != session.id
            or (
                instance.project_id is not None
                and session.project_id != instance.project_id
            )
        ):
            raise WorktreeOwnershipError("Worktree does not belong to the session")
        if instance.status == "active":
            intent = details.get("release_intent")
            if isinstance(intent, dict) and intent.get("session_id") == session.id:
                parent = (
                    await db.get(WorkspaceInstance, instance.parent_instance_id)
                    if instance.parent_instance_id is not None
                    else None
                )
                if (
                    parent is None
                    or intent.get("fallback_workspace_instance_id") != parent.id
                    or not isinstance(intent.get("token"), str)
                    or not intent["token"]
                    or not _same_resolved_path(session.directory, parent.root_path)
                ):
                    raise WorktreeConflictError(
                        "Reserved worktree release lost its fallback binding"
                    )
            elif not _same_resolved_path(session.directory, instance.root_path):
                raise WorktreeConflictError("Session is bound to a different workspace")
        if instance.status == "released":
            intent = details.get("release_intent")
            if (
                not isinstance(intent, dict)
                or intent.get("session_id") != session.id
                or not isinstance(intent.get("token"), str)
                or not intent["token"]
            ):
                raise WorktreeOwnershipError("Released worktree evidence is incomplete")
        return instance.status, details


def _gc_payload(report: Any) -> dict[str, Any]:
    # Error strings from Git/filesystem layers may contain local paths.  The
    # control plane exposes counts and owned identifiers, never raw diagnostics.
    return {
        "collected": list(report.collected),
        "blocked": list(report.blocked),
        "foreign": list(report.foreign),
        "error_count": len(report.errors),
        "complete": not report.blocked and not report.foreign and not report.errors,
    }


@router.get("/context")
async def runtime_context(
    session_factory: SessionFactoryDep,
    session_id: str = Query(min_length=1, max_length=128),
) -> Any:
    """Return the current path-free workspace identity for local controls."""

    from app import release_features

    if not release_features.V11_CHECKPOINTS_RELEASED:
        return _runtime_unavailable()
    try:
        _session, instance = await _current_workspace_context(
            session_factory,
            session_id=session_id,
        )
    except RewindNotFoundError:
        return _error(
            status_code=404,
            code="runtime_workspace_not_found",
            detail="The active session workspace was not found",
        )
    except RewindProvenanceError:
        return _error(
            status_code=409,
            code="runtime_workspace_provenance_mismatch",
            detail="The session workspace identity could not be verified",
        )
    return JSONResponse(
        content={
            "session_id": session_id,
            "workspace_instance_id": instance.id,
            "workspace_kind": instance.kind,
            "checkpoint_rewind_released": bool(
                release_features.V11_REWIND_RELEASED
            ),
            "managed_worktrees_released": bool(
                release_features.V11_WORKTREES_RELEASED
            ),
            "external_side_effects_reverted": False,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/checkpoints")
async def list_rewind_checkpoints(
    request: Request,
    session_factory: SessionFactoryDep,
    stream_manager: StreamManagerDep,
    session_id: str = Query(min_length=1, max_length=128),
    workspace_instance_id: str = Query(min_length=1, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
) -> Any:
    if not rewind_runtime_enabled():
        return _rewind_unavailable()
    service = _rewind_service(request, session_factory, stream_manager)
    try:
        await _require_current_checkpoint_workspace(
            session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
        )
        checkpoints = await service.list(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            limit=limit,
        )
        validation_summaries = await _checkpoint_validation_summaries(
            session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            checkpoint_ids=(item.checkpoint_id for item in checkpoints),
        )
    except RewindError as exc:
        return _rewind_error(exc)[1]
    return {
        "session_id": session_id,
        "workspace_instance_id": workspace_instance_id,
        "external_side_effects_are_reverted": False,
        "checkpoints": [
            {
                "checkpoint_id": item.checkpoint_id,
                "sequence": item.sequence,
                "state": item.state,
                "pin_state": item.pin_state,
                "anchor_message_id": item.anchor_message_id,
                "turn_run_id": item.turn_run_id,
                "has_irreversible_side_effects": item.has_irreversible_side_effects,
                "external_side_effects": _safe_external_effects(
                    item.external_side_effects
                ),
                "validation": validation_summaries[item.checkpoint_id],
            }
            for item in checkpoints
        ],
    }


@router.post("/rewind/preview")
async def preview_rewind(
    body: RewindRequest,
    request: Request,
    session_factory: SessionFactoryDep,
    stream_manager: StreamManagerDep,
) -> Any:
    if not rewind_runtime_enabled():
        return _rewind_unavailable()
    service = _rewind_service(request, session_factory, stream_manager)
    try:
        await _require_current_checkpoint_workspace(
            session_factory,
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
        preview = await service.preview(
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            checkpoint_id=body.checkpoint_id,
        )
    except RewindError as exc:
        return _rewind_error(exc)[1]
    return {
        "session_id": preview.session_id,
        "workspace_instance_id": preview.workspace_instance_id,
        "target_checkpoint_id": preview.target_checkpoint_id,
        "affected_checkpoint_ids": list(preview.affected_checkpoint_ids),
        "paths": [
            {
                "relative_path": item.relative_path,
                "action": item.action,
                "current_kind": item.current_kind,
                "desired_kind": item.desired_kind,
            }
            for item in preview.paths
        ],
        "conflicts": [
            {
                "relative_path": item.relative_path,
                "reason": item.reason,
            }
            for item in preview.conflicts
        ],
        "blockers": list(preview.blockers),
        "can_execute": preview.can_execute,
        "already_rewound": preview.already_rewound,
        "external_side_effects": _safe_external_effects(
            preview.external_side_effects
        ),
        "external_side_effects_will_be_reverted": False,
    }


@router.post("/rewind/execute")
async def execute_rewind(
    body: RewindRequest,
    request: Request,
    session_factory: SessionFactoryDep,
    stream_manager: StreamManagerDep,
) -> Any:
    if not rewind_runtime_enabled():
        return _rewind_unavailable()
    service = _rewind_service(request, session_factory, stream_manager)
    try:
        await _require_current_checkpoint_workspace(
            session_factory,
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
        await _record_mutation_audit(
            session_factory,
            capability="checkpoint_rewind",
            action="execute",
            outcome="started",
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            checkpoint_id=body.checkpoint_id,
            required=True,
        )
        result = await service.execute(
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            checkpoint_id=body.checkpoint_id,
        )
    except AuditPersistenceError:
        return _error(
            status_code=503,
            code="runtime_audit_unavailable",
            detail="The required pre-action audit record could not be persisted",
        )
    except RewindError as exc:
        code, response = _rewind_error(exc)
        await _record_mutation_audit(
            session_factory,
            capability="checkpoint_rewind",
            action="execute",
            outcome="blocked" if isinstance(exc, RewindBusyError) else "error",
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            checkpoint_id=body.checkpoint_id,
            machine_code=code,
        )
        return response
    await _record_mutation_audit(
        session_factory,
        capability="checkpoint_rewind",
        action="execute",
        outcome="success",
        session_id=body.session_id,
        workspace_instance_id=body.workspace_instance_id,
        checkpoint_id=body.checkpoint_id,
    )
    return {
        "status": "already_rewound" if result.already_rewound else "rewound",
        "already_rewound": result.already_rewound,
        "session_id": result.session_id,
        "workspace_instance_id": result.workspace_instance_id,
        "target_checkpoint_id": result.target_checkpoint_id,
        "affected_checkpoint_ids": list(result.affected_checkpoint_ids),
        "changed_paths": list(result.changed_paths),
        "messages_removed": result.messages_removed,
        "todos_restored": result.todos_restored,
        "external_side_effects": _safe_external_effects(
            result.external_side_effects
        ),
        "external_side_effects_were_reverted": False,
    }


@router.get("/worktrees/inspect")
async def inspect_worktree(
    request: Request,
    session_factory: SessionFactoryDep,
    stream_manager: StreamManagerDep,
    session_id: str = Query(min_length=1, max_length=128),
    workspace_instance_id: str = Query(min_length=1, max_length=128),
) -> Any:
    if not worktree_runtime_enabled():
        return _worktree_unavailable()
    runtime = _worktree_runtime(request, session_factory, stream_manager)
    try:
        status, details = await _worktree_db_state(
            session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
        )
        if status == "released":
            return {
                "session_id": session_id,
                "workspace_instance_id": workspace_instance_id,
                "database_status": status,
                "state": str(details.get("worktree_state", "removed")),
                "available": False,
                "clean": None,
                "registered": False,
                "head": None,
                "branch": None,
                "already_released": True,
            }
        inspection = await asyncio.to_thread(
            runtime.service.inspect, workspace_instance_id
        )
    except Exception as exc:
        return _worktree_error(exc)[1]
    return {
        "session_id": session_id,
        "workspace_instance_id": workspace_instance_id,
        "database_status": status,
        "state": inspection.record.state.value,
        "available": True,
        "clean": inspection.clean,
        "registered": inspection.registered,
        "head": inspection.head,
        "branch": inspection.branch,
        "already_released": False,
    }


@router.post("/worktrees/create-bind")
async def create_and_bind_worktree(
    body: WorktreeCreateRequest,
    request: Request,
    session_factory: SessionFactoryDep,
    stream_manager: StreamManagerDep,
) -> Any:
    if not worktree_runtime_enabled():
        return _worktree_unavailable()
    runtime = _worktree_runtime(request, session_factory, stream_manager)
    try:
        repository = await _session_repository(session_factory, body.session_id)
        await _record_mutation_audit(
            session_factory,
            capability="managed_worktree",
            action="create_bind",
            outcome="started",
            session_id=body.session_id,
            required=True,
        )
        binding = await runtime.create_and_bind_session(
            session_id=body.session_id,
            repository=repository,
            ref=body.ref,
            branch=body.branch,
        )
    except AuditPersistenceError:
        return _error(
            status_code=503,
            code="runtime_audit_unavailable",
            detail="The required pre-action audit record could not be persisted",
        )
    except Exception as exc:
        code, response = _worktree_error(exc)
        await _record_mutation_audit(
            session_factory,
            capability="managed_worktree",
            action="create_bind",
            outcome="blocked" if isinstance(exc, WorktreeActiveError) else "error",
            session_id=body.session_id,
            machine_code=code,
        )
        return response
    await _record_mutation_audit(
        session_factory,
        capability="managed_worktree",
        action="create_bind",
        outcome="success",
        session_id=body.session_id,
        workspace_instance_id=binding.workspace_instance_id,
    )
    return {
        "status": "bound",
        "session_id": binding.session_id,
        "workspace_instance_id": binding.workspace_instance_id,
        "parent_workspace_instance_id": binding.parent_workspace_instance_id,
        "state": binding.record.state.value,
        "head": binding.record.source_head,
        "branch": binding.record.branch,
        "clean": True,
    }


@router.post("/worktrees/release")
async def release_worktree(
    body: WorktreeInstanceRequest,
    request: Request,
    session_factory: SessionFactoryDep,
    stream_manager: StreamManagerDep,
) -> Any:
    if not worktree_runtime_enabled():
        return _worktree_unavailable()
    runtime = _worktree_runtime(request, session_factory, stream_manager)
    try:
        before_status, _details = await _worktree_db_state(
            session_factory,
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
        await _record_mutation_audit(
            session_factory,
            capability="managed_worktree",
            action="release",
            outcome="started",
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            required=True,
        )
        result = await runtime.release_session(
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
    except AuditPersistenceError:
        return _error(
            status_code=503,
            code="runtime_audit_unavailable",
            detail="The required pre-action audit record could not be persisted",
        )
    except Exception as exc:
        code, response = _worktree_error(exc)
        await _record_mutation_audit(
            session_factory,
            capability="managed_worktree",
            action="release",
            outcome="blocked"
            if isinstance(exc, (WorktreeActiveError, WorktreeDirtyError))
            else "error",
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            machine_code=code,
        )
        return response
    already_released = before_status == "released"
    await _record_mutation_audit(
        session_factory,
        capability="managed_worktree",
        action="release",
        outcome="success",
        session_id=body.session_id,
        workspace_instance_id=body.workspace_instance_id,
    )
    return {
        "status": "already_released" if already_released else "released",
        "already_released": already_released,
        "session_id": result.session_id,
        "workspace_instance_id": result.workspace_instance_id,
        "gc": _gc_payload(result.gc),
    }


@router.post("/worktrees/gc")
async def gc_worktree(
    body: WorktreeInstanceRequest,
    request: Request,
    session_factory: SessionFactoryDep,
    stream_manager: StreamManagerDep,
) -> Any:
    if not worktree_runtime_enabled():
        return _worktree_unavailable()
    runtime = _worktree_runtime(request, session_factory, stream_manager)
    try:
        status, _details = await _worktree_db_state(
            session_factory,
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
        if status != "released":
            raise WorktreeActiveError(
                "Garbage collection requires a released managed worktree"
            )
        await _record_mutation_audit(
            session_factory,
            capability="managed_worktree",
            action="gc",
            outcome="started",
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            required=True,
        )
        # release_session's released-state branch is the durable, idempotent
        # GC continuation and carries the exact persisted reservation token.
        result = await runtime.release_session(
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
    except AuditPersistenceError:
        return _error(
            status_code=503,
            code="runtime_audit_unavailable",
            detail="The required pre-action audit record could not be persisted",
        )
    except Exception as exc:
        code, response = _worktree_error(exc)
        await _record_mutation_audit(
            session_factory,
            capability="managed_worktree",
            action="gc",
            outcome="blocked" if isinstance(exc, WorktreeActiveError) else "error",
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            machine_code=code,
        )
        return response
    await _record_mutation_audit(
        session_factory,
        capability="managed_worktree",
        action="gc",
        outcome="success",
        session_id=body.session_id,
        workspace_instance_id=body.workspace_instance_id,
    )
    return {
        "status": "complete" if _gc_payload(result.gc)["complete"] else "retained",
        "session_id": result.session_id,
        "workspace_instance_id": result.workspace_instance_id,
        "gc": _gc_payload(result.gc),
    }


__all__ = ["router"]
