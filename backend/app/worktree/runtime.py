"""Database-coordinated lifecycle for app-owned Git worktrees.

``WorktreeService`` is intentionally synchronous and filesystem/Git focused.
This adapter supplies the missing persistent admission boundary: it reserves a
workspace in the database before detach/removal, makes new turn admission fail
closed, and only then authorizes the service's synchronous reference guard on
the exact worker thread performing the destructive operation.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import threading
from typing import TYPE_CHECKING, Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.turn_run import TurnRun
from app.models.workspace_instance import WorkspaceInstance
from app.storage.checkpoints import (
    CheckpointConflictError,
    CheckpointNotFoundError,
    register_workspace_instance,
    release_workspace_instance,
)
from app.utils.id import generate_ulid
from app.worktree.errors import (
    WorktreeActiveError,
    WorktreeConflictError,
    WorktreeFeatureDisabled,
)
from app.worktree.service import (
    GcReport,
    WorktreeRecord,
    WorktreeReferences,
    WorktreeService,
)

if TYPE_CHECKING:
    from app.streaming.manager import StreamManager


_RELEASE_INTENT_KEY = "release_intent"


def worktree_runtime_enabled() -> bool:
    """Worktrees depend on checkpoint ownership as well as their own gate."""

    from app import release_features

    return bool(
        release_features.V11_WORKTREES_RELEASED
        and release_features.V11_CHECKPOINTS_RELEASED
    )


@dataclass(frozen=True, slots=True)
class WorktreeBinding:
    session_id: str
    workspace_instance_id: str
    parent_workspace_instance_id: str
    checkout_path: str
    record: WorktreeRecord


@dataclass(frozen=True, slots=True)
class WorktreeReleaseResult:
    session_id: str
    workspace_instance_id: str
    fallback_workspace: str
    record: WorktreeRecord
    gc: GcReport


class WorktreeRuntimeReferenceGuard:
    """Authorize one exact DB-reserved release on its worker thread only.

    The guard deliberately cannot query an async database from synchronous Git
    code.  ``WorktreeRuntime`` first commits a durable ``release_intent`` that
    closes turn admission, then enters this thread-local capability while the
    service lock is held.  Calls outside that narrow scope fail closed.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    @contextmanager
    def authorize(
        self,
        *,
        workspace_instance_id: str,
        checkout_path: Path,
        reservation_token: str,
    ) -> Iterator[None]:
        if getattr(self._local, "authorization", None) is not None:
            raise WorktreeActiveError("Nested worktree release authorization is invalid")
        authorization = (
            str(workspace_instance_id),
            os.path.normcase(os.path.abspath(checkout_path)),
            str(reservation_token),
        )
        self._local.authorization = authorization
        try:
            yield
        finally:
            self._local.authorization = None

    def blockers_for(
        self,
        *,
        workspace_instance_id: str,
        checkout_path: Path,
    ) -> WorktreeReferences:
        authorization = getattr(self._local, "authorization", None)
        expected = (
            str(workspace_instance_id),
            os.path.normcase(os.path.abspath(checkout_path)),
        )
        if (
            not isinstance(authorization, tuple)
            or len(authorization) != 3
            or authorization[:2] != expected
            or not authorization[2]
        ):
            raise WorktreeActiveError(
                "Worktree release has no matching persistent reservation"
            )
        return WorktreeReferences()


class WorktreeRuntime:
    """Bind/release managed worktrees without a DB/Git TOCTOU window."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        service: WorktreeService,
        *,
        stream_manager: StreamManager | None = None,
        enabled: bool | None = None,
        reference_guard: WorktreeRuntimeReferenceGuard | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.service = service
        self.stream_manager = stream_manager
        self._enabled_override = enabled
        self.reference_guard = reference_guard or WorktreeRuntimeReferenceGuard()
        # The service is not exposed by this adapter until construction has
        # installed the reservation-aware guard.
        self.service.reference_guard = self.reference_guard

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return worktree_runtime_enabled()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise WorktreeFeatureDisabled(
                "Git worktree runtime is disabled by the v1.1 release gates"
            )
        if not self.service.enabled:
            raise WorktreeFeatureDisabled("Git worktree service is disabled")

    def _assert_memory_idle(self, session_id: str) -> None:
        if (
            self.stream_manager is not None
            and self.stream_manager.active_job_for_session(session_id) is not None
        ):
            raise WorktreeActiveError("Session still has an active generation job")

    @staticmethod
    async def _assert_database_idle(db: AsyncSession, session_id: str) -> None:
        active_turn = (
            await db.execute(
                select(TurnRun.id).where(
                    TurnRun.session_id == session_id,
                    TurnRun.status == "running",
                ).limit(1)
            )
        ).scalar_one_or_none()
        if active_turn is not None:
            raise WorktreeActiveError("Session still has a running turn")

    async def create_and_bind_session(
        self,
        *,
        session_id: str,
        repository: str | os.PathLike[str],
        ref: str = "HEAD",
        branch: str | None = None,
    ) -> WorktreeBinding:
        """Create a detached worktree and atomically move an idle session to it."""

        self._require_enabled()
        self._assert_memory_idle(session_id)
        repository_root = Path(repository).expanduser().resolve(strict=True)

        async with self.session_factory() as db:
            async with db.begin():
                session = await db.get(Session, session_id)
                if session is None:
                    raise CheckpointNotFoundError("Session not found")
                await self._assert_database_idle(db, session_id)
                try:
                    current_root = Path(session.directory).expanduser().resolve(strict=True)
                except OSError as exc:
                    raise WorktreeConflictError(
                        "Session workspace is unavailable"
                    ) from exc
                if current_root != repository_root:
                    raise WorktreeConflictError(
                        "Worktree source must equal the session's current workspace"
                    )
                parent = await register_workspace_instance(
                    db,
                    repository_root,
                    kind="direct",
                    project_id=session.project_id,
                    created_by_session_id=session.id,
                    details={"managed": False},
                )
                parent_id = parent.id

        instance_id = generate_ulid()
        record = await asyncio.to_thread(
            self.service.create,
            repository_root,
            workspace_instance_id=instance_id,
            ref=ref,
            branch=branch,
        )

        async with self.session_factory() as db:
            async with db.begin():
                session = await db.get(Session, session_id)
                if session is None:
                    raise CheckpointNotFoundError("Session disappeared during worktree create")
                await self._assert_database_idle(db, session_id)
                instance = await register_workspace_instance(
                    db,
                    record.checkout_path,
                    instance_id=instance_id,
                    kind="git_worktree",
                    project_id=session.project_id,
                    parent_instance_id=parent_id,
                    created_by_session_id=session.id,
                    details={
                        "managed": True,
                        "worktree_state": "created",
                        "session_id": session.id,
                        "repository_root": str(repository_root),
                    },
                )
                if instance.id != record.workspace_instance_id:
                    raise WorktreeConflictError(
                        "Git and database worktree identities diverged"
                    )

        bound = await asyncio.to_thread(
            self.service.bind,
            instance_id,
            expected_repository=repository_root,
        )
        self._assert_memory_idle(session_id)
        async with self.session_factory() as db:
            async with db.begin():
                session = await db.get(Session, session_id)
                instance = await db.get(WorkspaceInstance, instance_id)
                if session is None or instance is None:
                    raise CheckpointNotFoundError(
                        "Worktree binding database owner disappeared"
                    )
                await self._assert_database_idle(db, session_id)
                if instance.root_path != bound.checkout_path or instance.status != "active":
                    raise WorktreeConflictError(
                        "Worktree binding provenance changed before activation"
                    )
                details = dict(instance.details or {})
                details["worktree_state"] = "bound"
                instance.details = details
                session.directory = bound.checkout_path
                await db.flush()

        return WorktreeBinding(
            session_id=session_id,
            workspace_instance_id=instance_id,
            parent_workspace_instance_id=parent_id,
            checkout_path=bound.checkout_path,
            record=bound,
        )

    async def release_session(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
    ) -> WorktreeReleaseResult:
        """Move a session back to its parent, then detach/remove/GC its checkout.

        Failure after the database reservation is intentionally sticky: the
        session has already left the checkout and new turns cannot bind to it.
        Retrying this method resumes the idempotent service lifecycle.
        """

        self._require_enabled()
        self._assert_memory_idle(session_id)
        reservation_token: str
        checkout_path: str
        fallback_workspace: str
        already_released: WorktreeRecord | None = None

        async with self.session_factory() as db:
            async with db.begin():
                session = await db.get(Session, session_id)
                instance = await db.get(WorkspaceInstance, workspace_instance_id)
                if session is None:
                    raise CheckpointNotFoundError("Session not found")
                if instance is None:
                    raise CheckpointNotFoundError("Workspace instance not found")
                if instance.kind != "git_worktree":
                    raise WorktreeConflictError("Workspace instance is not a Git worktree")
                if not instance.parent_instance_id:
                    raise WorktreeConflictError("Worktree has no fallback workspace")
                parent = await db.get(WorkspaceInstance, instance.parent_instance_id)
                if parent is None:
                    raise WorktreeConflictError("Fallback workspace is unavailable")
                details = dict(instance.details or {})
                raw_intent = details.get(_RELEASE_INTENT_KEY)
                if instance.status == "released":
                    raw_record = details.get("worktree_record")
                    if (
                        not isinstance(raw_intent, dict)
                        or raw_intent.get("session_id") != session_id
                        or not isinstance(raw_intent.get("token"), str)
                        or not raw_intent["token"]
                        or not isinstance(raw_record, dict)
                    ):
                        raise WorktreeConflictError(
                            "Released worktree lacks its idempotency evidence"
                        )
                    already_released = WorktreeRecord.from_json(raw_record)
                    if already_released.workspace_instance_id != instance.id:
                        raise WorktreeConflictError(
                            "Released worktree evidence has different ownership"
                        )
                    reservation_token = raw_intent["token"]
                    checkout_path = instance.root_path
                    fallback_workspace = parent.root_path
                    if Path(session.directory).expanduser().resolve(strict=False) != Path(
                        fallback_workspace
                    ):
                        raise WorktreeConflictError(
                            "Released worktree session left its fallback workspace"
                        )
                    continue_release = False
                elif instance.status != "active":
                    raise WorktreeConflictError(
                        f"Workspace instance is already {instance.status}"
                    )
                else:
                    continue_release = True
                if continue_release:
                    if parent.status != "active":
                        raise WorktreeConflictError(
                            "Fallback workspace is unavailable"
                        )
                    await self._assert_database_idle(db, session_id)
                    pinned = (
                        await db.execute(
                            select(SessionCheckpoint.id).where(
                                SessionCheckpoint.workspace_instance_id == instance.id,
                                SessionCheckpoint.pin_state == "pinned",
                            ).limit(1)
                        )
                    ).scalar_one_or_none()
                    if pinned is not None:
                        raise WorktreeActiveError(
                            "Worktree still owns a rewindable checkpoint"
                        )

                    if raw_intent is None:
                        reservation_token = secrets.token_hex(32)
                        details[_RELEASE_INTENT_KEY] = {
                            "token": reservation_token,
                            "session_id": session_id,
                            "fallback_workspace_instance_id": parent.id,
                        }
                        details["worktree_state"] = "releasing"
                        instance.details = details
                    elif (
                        isinstance(raw_intent, dict)
                        and raw_intent.get("session_id") == session_id
                        and isinstance(raw_intent.get("token"), str)
                        and raw_intent["token"]
                    ):
                        reservation_token = raw_intent["token"]
                    else:
                        raise WorktreeConflictError(
                            "Worktree has a different persistent release reservation"
                        )
                    checkout_path = instance.root_path
                    fallback_workspace = parent.root_path
                    if Path(session.directory).expanduser().resolve(strict=False) not in {
                        Path(checkout_path),
                        Path(fallback_workspace),
                    }:
                        raise WorktreeConflictError(
                            "Session is bound to a different workspace"
                        )
                    session.directory = fallback_workspace
                    await db.flush()

        def collect_manifest(record: WorktreeRecord) -> GcReport:
            instance_root = self.service.managed_root / record.workspace_instance_id
            if not instance_root.exists():
                return GcReport()
            with self.reference_guard.authorize(
                workspace_instance_id=workspace_instance_id,
                checkout_path=Path(checkout_path),
                reservation_token=reservation_token,
            ):
                return self.service.gc(workspace_instance_id)

        if already_released is not None:
            gc = await asyncio.to_thread(collect_manifest, already_released)
            return WorktreeReleaseResult(
                session_id=session_id,
                workspace_instance_id=workspace_instance_id,
                fallback_workspace=fallback_workspace,
                record=already_released,
                gc=gc,
            )

        def release_filesystem() -> WorktreeRecord:
            with self.reference_guard.authorize(
                workspace_instance_id=workspace_instance_id,
                checkout_path=Path(checkout_path),
                reservation_token=reservation_token,
            ):
                self.service.detach(workspace_instance_id)
                removed = self.service.remove(workspace_instance_id)
                return removed

        record = await asyncio.to_thread(release_filesystem)

        async with self.session_factory() as db:
            async with db.begin():
                instance = await db.get(WorkspaceInstance, workspace_instance_id)
                if instance is None:
                    raise CheckpointNotFoundError("Workspace instance disappeared")
                intent = dict(instance.details or {}).get(_RELEASE_INTENT_KEY)
                if (
                    not isinstance(intent, dict)
                    or intent.get("token") != reservation_token
                ):
                    raise WorktreeConflictError(
                        "Worktree release reservation changed before completion"
                    )
                details = dict(instance.details or {})
                details["worktree_state"] = "removed"
                details["worktree_record"] = record.to_json()
                instance.details = details
                await release_workspace_instance(db, instance.id)

        gc = await asyncio.to_thread(collect_manifest, record)

        return WorktreeReleaseResult(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            fallback_workspace=fallback_workspace,
            record=record,
            gc=gc,
        )


__all__ = [
    "WorktreeBinding",
    "WorktreeReleaseResult",
    "WorktreeRuntime",
    "WorktreeRuntimeReferenceGuard",
    "worktree_runtime_enabled",
]
