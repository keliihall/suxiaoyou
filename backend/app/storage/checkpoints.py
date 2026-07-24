"""Transactional primitives for v1.1 root turns and rewind checkpoints.

The database ledger and app-private file-version manifest cannot participate in
one ACID transaction.  This module therefore gives every durable snapshot pin a
stable owner (``checkpoint:<id>``) and exposes reconciliation as a first-class
operation.  Runtime startup recovery can make the manifest converge to the
database source of truth after a crash at either persistence boundary.
"""

from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.checkpoint_change import CheckpointChange
from app.models.project import Project
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.turn_run import TurnRun
from app.models.workspace_instance import WorkspaceInstance
from app.storage.file_versions import FileVersionStore
from app.storage.workspace_identity import (
    WorkspaceIdentityError,
    ensure_workspace_identity as ensure_durable_workspace_identity,
    inspect_workspace_identity as inspect_durable_workspace_identity,
)
from app.utils.id import generate_ulid


TurnTerminalStatus = Literal["completed", "failed", "cancelled"]
CheckpointOperation = Literal["created", "modified", "deleted"]
CheckpointNodeKind = Literal["file", "directory", "symlink"]

_CHECKPOINT_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"committing", "failed"}),
    "committing": frozenset({"finalized", "failed"}),
    "finalized": frozenset({"rewinding"}),
    # ``finalized`` is the compensation edge for an intent whose filesystem
    # transaction never committed (preflight conflict, cancellation, or crash
    # recovery of a prepared journal).  It never applies after a committed
    # rewind journal exists; that journal must be completed idempotently.
    "rewinding": frozenset({"rewound", "finalized", "failed"}),
    "rewound": frozenset(),
    "failed": frozenset(),
}
_SHA256_LENGTH = hashlib.sha256().digest_size * 2


class CheckpointLedgerError(RuntimeError):
    """Base class for framework-independent checkpoint ledger failures."""


class CheckpointNotFoundError(CheckpointLedgerError):
    pass


class CheckpointConflictError(CheckpointLedgerError):
    pass


class CheckpointValidationError(CheckpointLedgerError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CheckpointValidationError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise CheckpointValidationError(f"{field} is required")
    if len(text) > maximum:
        raise CheckpointValidationError(
            f"{field} cannot exceed {maximum} characters"
        )
    return text


def _optional_text(value: Any, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckpointValidationError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > maximum:
        raise CheckpointValidationError(
            f"{field} cannot exceed {maximum} characters"
        )
    return text


def _sha256(value: str | None, *, field: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise CheckpointValidationError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise CheckpointValidationError(f"{field} must be a string")
    digest = value.strip().lower()
    if len(digest) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise CheckpointValidationError(f"{field} must be a SHA-256 digest")
    return digest


def _mode(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise CheckpointValidationError(f"{field} must be a non-negative integer")
    return value


def _json_object(value: dict[str, Any] | None, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CheckpointValidationError(f"{field} must be an object")
    return deepcopy(value)


def _relative_path(value: str) -> str:
    raw = _required_text(value, field="relative_path", maximum=4096)
    if "\\" in raw:
        raise CheckpointValidationError(
            "relative_path must use canonical forward slashes"
        )
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise CheckpointValidationError(
            "relative_path must be a canonical workspace-relative path"
        )
    return path.as_posix()


def inspect_workspace_identity(
    root_path: str | os.PathLike[str],
) -> tuple[str, str]:
    """Return a canonical directory path and replacement-resistant identity."""

    try:
        identity = inspect_durable_workspace_identity(root_path)
    except (OSError, WorkspaceIdentityError) as exc:
        raise CheckpointValidationError(
            f"Cannot inspect workspace directory: {root_path}"
        ) from exc
    return str(identity.canonical_path), identity.durable_token


def checkpoint_pin_owner(checkpoint_id: str) -> str:
    return f"checkpoint:{_required_text(checkpoint_id, field='checkpoint_id', maximum=200)}"


def _version_store(
    instance: WorkspaceInstance,
    supplied: FileVersionStore | None,
) -> FileVersionStore:
    store = supplied or FileVersionStore(
        instance.root_path,
        expected_durable_workspace_identity=instance.identity_token,
    )
    canonical, identity = inspect_workspace_identity(store.workspace)
    if canonical != instance.root_path or identity != instance.identity_token:
        raise CheckpointConflictError(
            "Workspace instance no longer identifies the file-version workspace"
        )
    return store


async def register_workspace_instance(
    db: AsyncSession,
    root_path: str | os.PathLike[str],
    *,
    instance_id: str | None = None,
    kind: str = "direct",
    project_id: str | None = None,
    parent_instance_id: str | None = None,
    created_by_session_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> WorkspaceInstance:
    """Get or create the durable identity for the current directory object."""

    try:
        durable_identity = ensure_durable_workspace_identity(root_path)
    except (OSError, WorkspaceIdentityError) as exc:
        raise CheckpointValidationError(
            f"Cannot establish workspace identity: {root_path}"
        ) from exc
    canonical = str(durable_identity.canonical_path)
    identity = durable_identity.durable_token
    normalized_instance_id = _optional_text(
        instance_id,
        field="instance_id",
        maximum=200,
    )
    if instance_id is not None and normalized_instance_id is None:
        raise CheckpointValidationError("instance_id must not be blank")
    normalized_kind = _required_text(kind, field="kind", maximum=40)
    existing = (
        await db.execute(
            select(WorkspaceInstance).where(
                WorkspaceInstance.root_path == canonical,
                WorkspaceInstance.identity_token == identity,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status != "active":
            raise CheckpointConflictError(
                "The same workspace filesystem instance was already released"
            )
        if (
            (
                normalized_instance_id is not None
                and existing.id != normalized_instance_id
            )
            or
            existing.kind != normalized_kind
            or existing.project_id != project_id
            or existing.parent_instance_id != parent_instance_id
        ):
            raise CheckpointConflictError(
                "Workspace identity is already registered with different provenance"
            )
        return existing

    if normalized_instance_id is not None:
        existing_id = await db.get(WorkspaceInstance, normalized_instance_id)
        if existing_id is not None:
            raise CheckpointConflictError(
                "Workspace instance ID is already registered to another identity"
            )

    if created_by_session_id is not None and await db.get(
        Session, created_by_session_id
    ) is None:
        raise CheckpointNotFoundError("Creating session not found")
    if project_id is not None and await db.get(Project, project_id) is None:
        raise CheckpointNotFoundError("Project not found")
    if parent_instance_id is not None:
        parent = await db.get(WorkspaceInstance, parent_instance_id)
        if parent is None:
            raise CheckpointNotFoundError("Parent workspace instance not found")
        if parent.status != "active":
            raise CheckpointConflictError("Parent workspace instance is not active")

    instance = WorkspaceInstance(
        id=normalized_instance_id or generate_ulid(),
        project_id=project_id,
        parent_instance_id=parent_instance_id,
        created_by_session_id=created_by_session_id,
        kind=normalized_kind,
        root_path=canonical,
        identity_token=identity,
        status="active",
        details=_json_object(details, field="details"),
    )
    db.add(instance)
    await db.flush()
    return instance


def _workspace_accepts_new_turn(instance: WorkspaceInstance) -> bool:
    details = instance.details if isinstance(instance.details, dict) else {}
    return instance.status == "active" and details.get("release_intent") is None


async def release_workspace_instance(
    db: AsyncSession,
    workspace_instance_id: str,
    *,
    missing: bool = False,
) -> WorkspaceInstance:
    """Finish an instance only after all execution and rewind pins are gone."""

    instance = await db.get(WorkspaceInstance, workspace_instance_id)
    if instance is None:
        raise CheckpointNotFoundError("Workspace instance not found")
    target = "missing" if missing else "released"
    if instance.status == target:
        return instance
    if instance.status != "active":
        raise CheckpointConflictError(
            f"Workspace instance is already {instance.status}"
        )
    active_turn = (
        await db.execute(
            select(TurnRun.id).where(
                TurnRun.workspace_instance_id == instance.id,
                TurnRun.status == "running",
            ).limit(1)
        )
    ).scalar_one_or_none()
    pinned_checkpoint = (
        await db.execute(
            select(SessionCheckpoint.id).where(
                SessionCheckpoint.workspace_instance_id == instance.id,
                SessionCheckpoint.pin_state == "pinned",
            ).limit(1)
        )
    ).scalar_one_or_none()
    if active_turn is not None or pinned_checkpoint is not None:
        raise CheckpointConflictError(
            "Workspace instance still has an active turn or rewindable checkpoint"
        )
    instance.status = target
    instance.time_released = _now()
    await db.flush()
    return instance


async def create_root_turn(
    db: AsyncSession,
    *,
    session_id: str,
    workspace_instance_id: str,
    source_kind: str = "interactive",
    turn_id: str | None = None,
    idempotency_key: str | None = None,
    request_message_id: str | None = None,
    stream_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> TurnRun:
    """Start a root turn whose own ID is the attribution root."""

    if await db.get(Session, session_id) is None:
        raise CheckpointNotFoundError("Session not found")
    instance = await db.get(WorkspaceInstance, workspace_instance_id)
    if instance is None:
        raise CheckpointNotFoundError("Workspace instance not found")
    normalized_key = _optional_text(
        idempotency_key, field="idempotency_key", maximum=160
    )
    normalized_source = _required_text(
        source_kind, field="source_kind", maximum=40
    )
    supplied_turn_id = _optional_text(turn_id, field="turn_id", maximum=200)
    normalized_stream_id = _optional_text(
        stream_id, field="stream_id", maximum=200
    )
    normalized_request_message_id = _optional_text(
        request_message_id, field="request_message_id", maximum=200
    )
    if turn_id is not None and supplied_turn_id is None:
        raise CheckpointValidationError("turn_id must not be blank")
    if supplied_turn_id is not None:
        existing_id = await db.get(TurnRun, supplied_turn_id)
        if existing_id is not None:
            if (
                existing_id.session_id != session_id
                or existing_id.id != existing_id.root_turn_id
                or existing_id.parent_turn_id is not None
                or existing_id.workspace_instance_id != workspace_instance_id
                or existing_id.source_kind != normalized_source
                or existing_id.idempotency_key != normalized_key
                or existing_id.stream_id != normalized_stream_id
                or existing_id.request_message_id != normalized_request_message_id
            ):
                raise CheckpointConflictError(
                    "Supplied root turn ID was used with different provenance"
                )
            return existing_id
    if normalized_key is not None:
        replay = (
            await db.execute(
                select(TurnRun).where(
                    TurnRun.session_id == session_id,
                    TurnRun.idempotency_key == normalized_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if (
                replay.parent_turn_id is not None
                or replay.workspace_instance_id != workspace_instance_id
                or replay.source_kind != normalized_source
                or (
                    supplied_turn_id is not None and replay.id != supplied_turn_id
                )
                or replay.stream_id != normalized_stream_id
                or replay.request_message_id != normalized_request_message_id
            ):
                raise CheckpointConflictError(
                    "Turn idempotency key was used with different provenance"
                )
            return replay

    if not _workspace_accepts_new_turn(instance):
        raise CheckpointConflictError(
            "Workspace instance is not accepting new turns"
        )

    resolved_turn_id = supplied_turn_id or generate_ulid()
    turn = TurnRun(
        id=resolved_turn_id,
        session_id=session_id,
        workspace_instance_id=workspace_instance_id,
        root_turn_id=resolved_turn_id,
        parent_turn_id=None,
        depth=0,
        source_kind=normalized_source,
        status="running",
        idempotency_key=normalized_key,
        request_message_id=normalized_request_message_id,
        stream_id=normalized_stream_id,
        external_side_effects=[],
        details=_json_object(details, field="details"),
        time_started=_now(),
    )
    db.add(turn)
    await db.flush()
    return turn


async def create_child_turn(
    db: AsyncSession,
    *,
    parent_turn_id: str,
    session_id: str,
    workspace_instance_id: str | None = None,
    source_kind: str = "subagent",
    turn_id: str | None = None,
    idempotency_key: str | None = None,
    request_message_id: str | None = None,
    stream_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> TurnRun:
    """Start a delegated run while preserving the original root attribution."""

    parent = await db.get(TurnRun, parent_turn_id)
    if parent is None:
        raise CheckpointNotFoundError("Parent turn not found")
    target_workspace_id = workspace_instance_id or parent.workspace_instance_id
    return await _create_child_turn(
        db,
        parent=parent,
        session_id=session_id,
        workspace_instance_id=target_workspace_id,
        source_kind=source_kind,
        turn_id=turn_id,
        idempotency_key=idempotency_key,
        request_message_id=request_message_id,
        stream_id=stream_id,
        details=details,
    )


async def _create_child_turn(
    db: AsyncSession,
    *,
    parent: TurnRun,
    session_id: str,
    workspace_instance_id: str,
    source_kind: str,
    turn_id: str | None,
    idempotency_key: str | None,
    request_message_id: str | None,
    stream_id: str | None,
    details: dict[str, Any] | None,
) -> TurnRun:
    if await db.get(Session, session_id) is None:
        raise CheckpointNotFoundError("Child session not found")
    instance = await db.get(WorkspaceInstance, workspace_instance_id)
    if instance is None:
        raise CheckpointNotFoundError("Child workspace instance not found")
    normalized_key = _optional_text(
        idempotency_key, field="idempotency_key", maximum=160
    )
    normalized_source = _required_text(
        source_kind, field="source_kind", maximum=40
    )
    supplied_turn_id = _optional_text(turn_id, field="turn_id", maximum=200)
    normalized_stream_id = _optional_text(
        stream_id, field="stream_id", maximum=200
    )
    normalized_request_message_id = _optional_text(
        request_message_id, field="request_message_id", maximum=200
    )
    if turn_id is not None and supplied_turn_id is None:
        raise CheckpointValidationError("turn_id must not be blank")
    if supplied_turn_id is not None:
        existing_id = await db.get(TurnRun, supplied_turn_id)
        if existing_id is not None:
            if (
                existing_id.session_id != session_id
                or existing_id.parent_turn_id != parent.id
                or existing_id.root_turn_id != parent.root_turn_id
                or existing_id.workspace_instance_id != workspace_instance_id
                or existing_id.source_kind != normalized_source
                or existing_id.idempotency_key != normalized_key
                or existing_id.stream_id != normalized_stream_id
                or existing_id.request_message_id != normalized_request_message_id
            ):
                raise CheckpointConflictError(
                    "Supplied child turn ID was used with different provenance"
                )
            return existing_id
    if normalized_key is not None:
        replay = (
            await db.execute(
                select(TurnRun).where(
                    TurnRun.session_id == session_id,
                    TurnRun.idempotency_key == normalized_key,
                )
            )
        ).scalar_one_or_none()
        if replay is not None:
            if (
                replay.parent_turn_id != parent.id
                or replay.root_turn_id != parent.root_turn_id
                or replay.workspace_instance_id != workspace_instance_id
                or replay.source_kind != normalized_source
                or (
                    supplied_turn_id is not None and replay.id != supplied_turn_id
                )
                or replay.stream_id != normalized_stream_id
                or replay.request_message_id != normalized_request_message_id
            ):
                raise CheckpointConflictError(
                    "Turn idempotency key was used with different provenance"
                )
            return replay

    if parent.status != "running":
        raise CheckpointConflictError("Cannot attach a child to a finished turn")
    if not _workspace_accepts_new_turn(instance):
        raise CheckpointConflictError(
            "Child workspace instance is not accepting new turns"
        )

    turn = TurnRun(
        id=supplied_turn_id or generate_ulid(),
        session_id=session_id,
        workspace_instance_id=workspace_instance_id,
        root_turn_id=parent.root_turn_id,
        parent_turn_id=parent.id,
        depth=parent.depth + 1,
        source_kind=normalized_source,
        status="running",
        idempotency_key=normalized_key,
        request_message_id=normalized_request_message_id,
        stream_id=normalized_stream_id,
        external_side_effects=[],
        details=_json_object(details, field="details"),
        time_started=_now(),
    )
    db.add(turn)
    await db.flush()
    return turn


async def finish_turn(
    db: AsyncSession,
    turn_run_id: str,
    *,
    status: TurnTerminalStatus,
    response_message_id: str | None = None,
) -> TurnRun:
    turn = await db.get(TurnRun, turn_run_id)
    if turn is None:
        raise CheckpointNotFoundError("Turn not found")
    if status not in {"completed", "failed", "cancelled"}:
        raise CheckpointValidationError("Invalid terminal turn status")
    if turn.status != "running":
        if turn.status == status:
            return turn
        raise CheckpointConflictError(f"Turn is already {turn.status}")
    turn.status = status
    turn.response_message_id = _optional_text(
        response_message_id, field="response_message_id", maximum=200
    )
    turn.time_finished = _now()
    await db.flush()
    return turn


async def prepare_checkpoint(
    db: AsyncSession,
    *,
    turn_run_id: str,
    anchor_message_id: str | None,
    goal_run_id: str | None = None,
    todo_snapshot: list[dict[str, Any]] | None = None,
    details: dict[str, Any] | None = None,
) -> SessionCheckpoint:
    """Persist the pre-commit boundary for one root or delegated turn."""

    turn = await db.get(TurnRun, turn_run_id)
    if turn is None:
        raise CheckpointNotFoundError("Turn not found")
    existing = (
        await db.execute(
            select(SessionCheckpoint).where(
                SessionCheckpoint.turn_run_id == turn.id
            )
        )
    ).scalar_one_or_none()
    normalized_anchor = _optional_text(
        anchor_message_id, field="anchor_message_id", maximum=200
    )
    normalized_goal_run = _optional_text(
        goal_run_id, field="goal_run_id", maximum=200
    )
    if todo_snapshot is not None and not isinstance(todo_snapshot, list):
        raise CheckpointValidationError("todo_snapshot must be a list")
    normalized_todos = deepcopy(todo_snapshot or [])
    if not all(isinstance(item, dict) for item in normalized_todos):
        raise CheckpointValidationError("todo_snapshot must contain objects")
    if existing is not None:
        if (
            existing.anchor_message_id != normalized_anchor
            or existing.goal_run_id != normalized_goal_run
            or existing.todo_snapshot != normalized_todos
        ):
            raise CheckpointConflictError(
                "Turn checkpoint was prepared with a different boundary"
            )
        return existing

    next_sequence = int(
        (
            await db.execute(
                select(func.coalesce(func.max(SessionCheckpoint.sequence), 0)).where(
                    SessionCheckpoint.session_id == turn.session_id
                )
            )
        ).scalar_one()
    ) + 1
    checkpoint = SessionCheckpoint(
        id=generate_ulid(),
        session_id=turn.session_id,
        workspace_instance_id=turn.workspace_instance_id,
        root_turn_id=turn.root_turn_id,
        turn_run_id=turn.id,
        sequence=next_sequence,
        anchor_message_id=normalized_anchor,
        goal_run_id=normalized_goal_run,
        todo_snapshot=normalized_todos,
        child_turn_ids=[],
        state="prepared",
        pin_state="pinned",
        external_side_effects=[],
        details=_json_object(details, field="details"),
    )
    db.add(checkpoint)
    await db.flush()
    return checkpoint


async def get_checkpoint(
    db: AsyncSession, checkpoint_id: str
) -> SessionCheckpoint | None:
    return await db.get(SessionCheckpoint, checkpoint_id)


async def list_session_checkpoints(
    db: AsyncSession,
    session_id: str,
    *,
    limit: int = 100,
) -> list[SessionCheckpoint]:
    if limit < 1 or limit > 500:
        raise CheckpointValidationError("limit must be between 1 and 500")
    return list(
        (
            await db.execute(
                select(SessionCheckpoint)
                .where(SessionCheckpoint.session_id == session_id)
                .order_by(SessionCheckpoint.sequence.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def list_root_turn_checkpoints(
    db: AsyncSession,
    root_turn_id: str,
) -> list[SessionCheckpoint]:
    """List every root/child checkpoint that a root rewind must aggregate."""

    return list(
        (
            await db.execute(
                select(SessionCheckpoint)
                .join(TurnRun, TurnRun.id == SessionCheckpoint.turn_run_id)
                .where(SessionCheckpoint.root_turn_id == root_turn_id)
                .order_by(TurnRun.depth, SessionCheckpoint.time_created)
            )
        ).scalars()
    )


async def _descendant_turn_ids(
    db: AsyncSession,
    owner_turn_id: str,
    root_turn_id: str,
) -> list[str]:
    rows = (
        await db.execute(
            select(
                TurnRun.id,
                TurnRun.parent_turn_id,
                TurnRun.depth,
                TurnRun.time_created,
            ).where(TurnRun.root_turn_id == root_turn_id)
        )
    ).all()
    descendants = {owner_turn_id}
    remaining = list(rows)
    changed = True
    while changed:
        changed = False
        for turn_id, parent_id, _depth, _created in remaining:
            if turn_id not in descendants and parent_id in descendants:
                descendants.add(turn_id)
                changed = True
    return [
        turn_id
        for turn_id, _parent_id, _depth, _created in sorted(
            remaining,
            key=lambda row: (row[2], row[3], row[0]),
        )
        if turn_id != owner_turn_id and turn_id in descendants
    ]


async def transition_checkpoint(
    db: AsyncSession,
    checkpoint_id: str,
    *,
    target_state: str,
) -> SessionCheckpoint:
    checkpoint = await db.get(SessionCheckpoint, checkpoint_id)
    if checkpoint is None:
        raise CheckpointNotFoundError("Checkpoint not found")
    target = str(target_state).strip()
    if target == checkpoint.state:
        return checkpoint
    if target not in _CHECKPOINT_TRANSITIONS.get(checkpoint.state, frozenset()):
        raise CheckpointConflictError(
            f"Cannot transition checkpoint from {checkpoint.state} to {target}"
        )
    if target == "rewinding" and checkpoint.pin_state != "pinned":
        raise CheckpointConflictError("Released checkpoint versions cannot be rewound")
    checkpoint.state = target
    now = _now()
    if target == "finalized":
        if checkpoint.time_finalized is None:
            checkpoint.time_finalized = now
            checkpoint.child_turn_ids = await _descendant_turn_ids(
                db,
                checkpoint.turn_run_id,
                checkpoint.root_turn_id,
            )
    elif target == "rewound":
        checkpoint.time_rewound = now
    await db.flush()
    return checkpoint


async def record_checkpoint_change(
    db: AsyncSession,
    *,
    checkpoint_id: str,
    turn_run_id: str,
    operation: CheckpointOperation,
    node_kind: CheckpointNodeKind,
    relative_path: str,
    before_version_id: str | None = None,
    before_sha256: str | None = None,
    before_mode: int | None = None,
    after_sha256: str | None = None,
    after_mode: int | None = None,
    after_size: int | None = None,
    call_id: str | None = None,
    details: dict[str, Any] | None = None,
    version_store: FileVersionStore | None = None,
) -> CheckpointChange:
    """Append a planned/committed mutation while its checkpoint is committing.

    Rename is intentionally represented as an ordered ``deleted`` plus
    ``created`` pair so each target retains unambiguous before/after evidence.
    """

    checkpoint = await db.get(SessionCheckpoint, checkpoint_id)
    if checkpoint is None:
        raise CheckpointNotFoundError("Checkpoint not found")
    if checkpoint.state != "committing" or checkpoint.pin_state != "pinned":
        raise CheckpointConflictError(
            "Checkpoint changes can only be recorded while committing and pinned"
        )
    turn = await db.get(TurnRun, turn_run_id)
    if turn is None:
        raise CheckpointNotFoundError("Turn not found")
    if turn.id != checkpoint.turn_run_id:
        raise CheckpointConflictError("Turn does not own this checkpoint")
    if turn.workspace_instance_id != checkpoint.workspace_instance_id:
        raise CheckpointConflictError(
            "Turn belongs to a different workspace instance"
        )
    if operation not in {"created", "modified", "deleted"}:
        raise CheckpointValidationError("Invalid checkpoint operation")
    if node_kind not in {"file", "directory", "symlink"}:
        raise CheckpointValidationError("Invalid checkpoint node kind")

    normalized_path = _relative_path(relative_path)
    created = operation == "created"
    deleted = operation == "deleted"
    normalized_before_mode = _mode(before_mode, field="before_mode")
    normalized_after_mode = _mode(after_mode, field="after_mode")
    if after_size is not None and (type(after_size) is not int or after_size < 0):
        raise CheckpointValidationError("after_size must be a non-negative integer")
    normalized_after_size = after_size
    normalized_details = _json_object(details, field="details")

    normalized_before_version = _optional_text(
        before_version_id, field="before_version_id", maximum=200
    )
    if node_kind == "directory":
        if any(
            value is not None
            for value in (
                normalized_before_version,
                before_sha256,
                after_sha256,
                after_size,
            )
        ):
            raise CheckpointValidationError(
                "Directory changes cannot contain file versions, hashes, or sizes"
            )
        normalized_before_sha = None
        normalized_after_sha = None
    elif node_kind == "symlink":
        if operation != "created":
            raise CheckpointValidationError(
                "Only newly created symbolic links are supported"
            )
        if normalized_before_version is not None or before_sha256 is not None:
            raise CheckpointValidationError(
                "Created symbolic links must explicitly have no before state"
            )
        raw_target = normalized_details.get("link_target")
        if not isinstance(raw_target, str) or not raw_target or len(raw_target) > 4096:
            raise CheckpointValidationError(
                "Created symbolic links require a non-empty details.link_target"
            )
        if "\x00" in raw_target:
            raise CheckpointValidationError("Symbolic-link target contains NUL")
        try:
            target_bytes = raw_target.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CheckpointValidationError(
                "Symbolic-link target must be valid UTF-8 text"
            ) from exc
        normalized_before_sha = None
        normalized_after_sha = _sha256(
            after_sha256,
            field="after_sha256",
            required=True,
        )
        expected_link_sha = hashlib.sha256(target_bytes).hexdigest()
        if normalized_after_sha != expected_link_sha:
            raise CheckpointValidationError(
                "Symbolic-link SHA-256 must hash the UTF-8 link target"
            )
        if normalized_after_size is not None and normalized_after_size != len(
            target_bytes
        ):
            raise CheckpointValidationError(
                "Symbolic-link after_size must equal the UTF-8 target length"
            )
        normalized_after_size = len(target_bytes)
        instance = await db.get(WorkspaceInstance, checkpoint.workspace_instance_id)
        if instance is None:
            raise CheckpointNotFoundError("Workspace instance not found")
        link = Path(instance.root_path) / normalized_path
        target_path = Path(raw_target)
        resolved_target = (
            target_path.resolve(strict=False)
            if target_path.is_absolute()
            else (link.parent / target_path).resolve(strict=False)
        )
        try:
            resolved_target.relative_to(Path(instance.root_path))
        except ValueError as exc:
            raise CheckpointValidationError(
                "Symbolic-link target resolves outside the workspace"
            ) from exc
    else:
        normalized_before_sha = _sha256(
            before_sha256,
            field="before_sha256",
            required=not created,
        )
        normalized_after_sha = _sha256(
            after_sha256,
            field="after_sha256",
            required=not deleted,
        )
        if created:
            if normalized_before_version is not None or normalized_before_mode is not None:
                raise CheckpointValidationError(
                    "Created files must explicitly have no before state"
                )
        elif normalized_before_version is None:
            raise CheckpointValidationError(
                "Modified/deleted files require a before version ID"
            )
        if deleted and (
            normalized_after_mode is not None or normalized_after_size is not None
        ):
            raise CheckpointValidationError(
                "Deleted files cannot contain after mode or size"
            )

    if created and normalized_before_mode is not None:
        raise CheckpointValidationError("Created paths cannot have a before mode")
    if deleted and normalized_after_mode is not None:
        raise CheckpointValidationError("Deleted paths cannot have an after mode")

    pinned_ids: frozenset[str] = frozenset()
    store: FileVersionStore | None = None
    if normalized_before_version is not None:
        instance = await db.get(WorkspaceInstance, checkpoint.workspace_instance_id)
        if instance is None:
            raise CheckpointNotFoundError("Workspace instance not found")
        store = _version_store(instance, version_store)
        version = store.get_version(normalized_before_version)
        if version.relative_path != normalized_path:
            raise CheckpointValidationError(
                "Before version belongs to a different workspace path"
            )
        if version.sha256 != normalized_before_sha:
            raise CheckpointValidationError(
                "Before SHA-256 does not match the immutable file version"
            )
        if (
            normalized_before_mode is not None
            and version.original_mode is not None
            and normalized_before_mode != version.original_mode
        ):
            raise CheckpointValidationError(
                "Before mode does not match the immutable file version"
            )
        pinned_ids = store.pin_versions(
            checkpoint_pin_owner(checkpoint.id),
            [normalized_before_version],
        )

    next_sequence = int(
        (
            await db.execute(
                select(func.coalesce(func.max(CheckpointChange.sequence), 0)).where(
                    CheckpointChange.checkpoint_id == checkpoint.id
                )
            )
        ).scalar_one()
    ) + 1
    change = CheckpointChange(
        id=generate_ulid(),
        checkpoint_id=checkpoint.id,
        turn_run_id=turn.id,
        sequence=next_sequence,
        operation=operation,
        node_kind=node_kind,
        relative_path=normalized_path,
        before_exists=not created,
        before_version_id=normalized_before_version,
        before_sha256=normalized_before_sha,
        before_mode=normalized_before_mode,
        after_exists=not deleted,
        after_sha256=normalized_after_sha,
        after_mode=normalized_after_mode,
        after_size=normalized_after_size,
        call_id=_optional_text(call_id, field="call_id", maximum=200),
        details=normalized_details,
    )
    db.add(change)
    try:
        await db.flush()
    except Exception:
        if store is not None and pinned_ids:
            store.unpin_versions(
                checkpoint_pin_owner(checkpoint.id),
                pinned_ids,
            )
        raise
    return change


async def list_checkpoint_changes(
    db: AsyncSession, checkpoint_id: str
) -> list[CheckpointChange]:
    return list(
        (
            await db.execute(
                select(CheckpointChange)
                .where(CheckpointChange.checkpoint_id == checkpoint_id)
                .order_by(CheckpointChange.sequence)
            )
        ).scalars()
    )


async def record_irreversible_side_effect(
    db: AsyncSession,
    *,
    checkpoint_id: str,
    turn_run_id: str,
    source: str,
    operation: str,
    audit_id: str,
) -> SessionCheckpoint:
    """Record a redacted external effect; never claim it can be rewound."""

    checkpoint = await db.get(SessionCheckpoint, checkpoint_id)
    turn = await db.get(TurnRun, turn_run_id)
    if checkpoint is None:
        raise CheckpointNotFoundError("Checkpoint not found")
    if turn is None:
        raise CheckpointNotFoundError("Turn not found")
    if turn.id != checkpoint.turn_run_id:
        raise CheckpointConflictError("Turn does not own this checkpoint")
    if checkpoint.state in {"rewound", "failed"}:
        raise CheckpointConflictError(
            "Cannot add an external effect to a terminal checkpoint"
        )
    summary = {
        "source": _required_text(source, field="source", maximum=80),
        "operation": _required_text(operation, field="operation", maximum=80),
        "audit_id": _required_text(audit_id, field="audit_id", maximum=200),
    }
    checkpoint_effects = list(checkpoint.external_side_effects or [])
    if summary not in checkpoint_effects:
        checkpoint.external_side_effects = [*checkpoint_effects, summary]
    checkpoint.has_irreversible_side_effects = True

    turn_effects = list(turn.external_side_effects or [])
    if summary not in turn_effects:
        turn.external_side_effects = [*turn_effects, summary]
    turn.has_irreversible_side_effects = True
    if turn.id != turn.root_turn_id:
        root = await db.get(TurnRun, turn.root_turn_id)
        if root is None:
            raise CheckpointConflictError("Root turn ledger is missing")
        root_effects = list(root.external_side_effects or [])
        if summary not in root_effects:
            root.external_side_effects = [*root_effects, summary]
        root.has_irreversible_side_effects = True
    await db.flush()
    return checkpoint


async def release_checkpoint_pin(
    db: AsyncSession,
    checkpoint_id: str,
    *,
    version_store: FileVersionStore | None = None,
) -> SessionCheckpoint:
    """Irreversibly release retention after finalization/recovery/expiry."""

    checkpoint = await db.get(SessionCheckpoint, checkpoint_id)
    if checkpoint is None:
        raise CheckpointNotFoundError("Checkpoint not found")
    if checkpoint.pin_state == "released":
        return checkpoint
    if checkpoint.state not in {"finalized", "rewound", "failed"}:
        raise CheckpointConflictError(
            "Checkpoint pin cannot be released before a terminal persistence state"
        )
    instance = await db.get(WorkspaceInstance, checkpoint.workspace_instance_id)
    if instance is None:
        raise CheckpointNotFoundError("Workspace instance not found")
    store = _version_store(instance, version_store)
    released_at = _now()
    checkpoint.pin_state = "released"
    checkpoint.time_pin_released = released_at
    await db.flush()
    try:
        store.unpin_versions(checkpoint_pin_owner(checkpoint.id))
    except Exception:
        checkpoint.pin_state = "pinned"
        checkpoint.time_pin_released = None
        await db.flush()
        raise
    return checkpoint


async def reconcile_workspace_checkpoint_pins(
    db: AsyncSession,
    workspace_instance_id: str,
    *,
    version_store: FileVersionStore | None = None,
) -> int:
    """Converge manifest owners to all currently pinned DB checkpoints."""

    instance = await db.get(WorkspaceInstance, workspace_instance_id)
    if instance is None:
        raise CheckpointNotFoundError("Workspace instance not found")
    store = _version_store(instance, version_store)
    checkpoints = list(
        (
            await db.execute(
                select(SessionCheckpoint).where(
                    SessionCheckpoint.workspace_instance_id == instance.id
                )
            )
        ).scalars()
    )
    expected: dict[str, set[str]] = {}
    for checkpoint in checkpoints:
        owner = checkpoint_pin_owner(checkpoint.id)
        if checkpoint.pin_state != "pinned":
            continue
        version_ids = set(
            (
                await db.execute(
                    select(CheckpointChange.before_version_id).where(
                        CheckpointChange.checkpoint_id == checkpoint.id,
                        CheckpointChange.before_version_id.is_not(None),
                    )
                )
            ).scalars()
        )
        expected[owner] = {value for value in version_ids if value is not None}

    changes = 0
    actual = store.list_pins()
    for owner, version_ids in expected.items():
        if actual.get(owner, frozenset()) != frozenset(version_ids):
            store.replace_pinned_versions(owner, version_ids)
            changes += 1
    for owner in actual:
        if owner.startswith("checkpoint:") and owner not in expected:
            store.unpin_versions(owner)
            changes += 1
    return changes
