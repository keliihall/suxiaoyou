"""Durable, conflict-safe v1.1 session rewind service.

Rewind is deliberately a server-owned operation.  A caller must name both the
session and the concrete workspace instance; neither a model-supplied path nor
the current process directory is accepted as provenance.  File changes are
assembled in :class:`WorkspaceMutationTransaction`'s private stage and become
visible only after the complete ledger has passed an exact preflight.

The database intent is persisted before the filesystem commit.  A committed
schema-4 rewind journal then bridges the filesystem/database boundary so
startup recovery can finish the conversation and retention updates
idempotently after a hard process exit.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any, AsyncIterator, Iterable, Literal, Protocol

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.checkpoint_change import CheckpointChange
from app.models.goal_run import GoalRun
from app.models.message import Message
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.session_goal import SessionGoal
from app.models.todo import Todo
from app.models.turn_run import TurnRun
from app.models.workspace_instance import WorkspaceInstance
from app.schemas.agent import AgentInfo
from app.session.manager import invalidate_acp_prompt_ledgers_for_messages
from app.storage.checkpoints import inspect_workspace_identity, transition_checkpoint
from app.storage.file_versions import FileVersionError, FileVersionStore
from app.tool.context import ToolContext
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
    cleanup_committed_checkpoint_journal,
    committed_checkpoint_journal_action,
    list_committed_checkpoint_journals,
)


_ACTIVE_GOAL_RUN_STATES = frozenset({"reserved", "running", "waiting_user"})
_REWINDABLE_CHECKPOINT_STATES = frozenset({"finalized", "rewound"})
_HASH_CHUNK = 1024 * 1024
_MAX_AFFECTED_CHECKPOINTS = 500
_MAX_LEDGER_CHANGES = 50_000
_MAX_TODOS = 10_000
logger = logging.getLogger(__name__)


class _StreamManager(Protocol):
    job_admission_lock: asyncio.Lock

    def active_job_for_session(self, session_id: str) -> Any | None: ...


class RewindError(RuntimeError):
    """Base error for a rewind request that was not completed."""


class RewindDisabledError(RewindError):
    pass


class RewindNotFoundError(RewindError):
    pass


class RewindProvenanceError(RewindError):
    pass


class RewindBusyError(RewindError):
    pass


class RewindConflictError(RewindError):
    def __init__(self, message: str, *, conflicts: Iterable["RewindConflict"] = ()):
        super().__init__(message)
        self.conflicts = tuple(conflicts)


@dataclass(frozen=True, slots=True)
class RewindConflict:
    relative_path: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class RewindCheckpointItem:
    checkpoint_id: str
    sequence: int
    state: str
    pin_state: str
    anchor_message_id: str | None
    turn_run_id: str
    has_irreversible_side_effects: bool
    external_side_effects: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RewindPath:
    relative_path: str
    action: Literal["restore_file", "create_directory", "remove", "none"]
    current_kind: str | None
    desired_kind: str | None
    source_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class RewindPreview:
    session_id: str
    workspace_instance_id: str
    target_checkpoint_id: str
    affected_checkpoint_ids: tuple[str, ...]
    paths: tuple[RewindPath, ...]
    conflicts: tuple[RewindConflict, ...]
    blockers: tuple[str, ...]
    external_side_effects: tuple[dict[str, Any], ...]
    already_rewound: bool = False

    @property
    def can_execute(self) -> bool:
        return self.already_rewound or (not self.conflicts and not self.blockers)


@dataclass(frozen=True, slots=True)
class RewindResult:
    session_id: str
    workspace_instance_id: str
    target_checkpoint_id: str
    affected_checkpoint_ids: tuple[str, ...]
    changed_paths: tuple[str, ...]
    messages_removed: int
    todos_restored: int
    external_side_effects: tuple[dict[str, Any], ...]
    already_rewound: bool = False


@dataclass(frozen=True, slots=True)
class _State:
    exists: bool
    kind: Literal["file", "directory", "symlink"] | None = None
    mode: int | None = None
    sha256: str | None = None
    size: int | None = None
    link_target: str | None = None
    # Retention provenance is not part of the visible filesystem state.  It is
    # needed only when materializing the desired file into the private stage.
    version_id: str | None = field(default=None, compare=False)


_ABSENT = _State(exists=False)


@dataclass(frozen=True, slots=True)
class _Change:
    checkpoint_id: str
    checkpoint_sequence: int
    sequence: int
    relative_path: str
    operation: str
    node_kind: str
    before_version_id: str | None
    before_sha256: str | None
    before_mode: int | None
    after_sha256: str | None
    after_mode: int | None
    after_size: int | None
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PlanEntry:
    relative_path: str
    final: _State
    desired: _State


@dataclass(frozen=True, slots=True)
class _RewindPlan:
    session_id: str
    workspace_instance_id: str
    workspace_identity_token: str
    workspace_root: str
    target_checkpoint_id: str
    target_turn_run_id: str
    target_root_turn_id: str
    anchor_message_id: str
    affected_checkpoint_ids: tuple[str, ...]
    affected_turn_ids: tuple[str, ...]
    todo_snapshot: tuple[dict[str, Any], ...]
    goal_id: str | None
    goal_run_id: str | None
    goal_stream_id: str | None
    entries: tuple[_PlanEntry, ...]
    external_side_effects: tuple[dict[str, Any], ...]


def rewind_runtime_enabled() -> bool:
    """Read the release switch dynamically; production defaults remain closed."""

    from app import release_features

    return bool(
        release_features.V11_CHECKPOINTS_RELEASED
        and release_features.V11_REWIND_RELEASED
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_relative(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or "\x00" in value
    ):
        raise RewindConflictError("Checkpoint ledger path is not canonical")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise RewindConflictError("Checkpoint ledger path escapes the workspace")
    if path.parts[0] == ".suxiaoyou":
        raise RewindConflictError("Checkpoint ledger targets app-private workspace state")
    return path.as_posix()


def _copy_change(change: CheckpointChange, checkpoint_sequence: int) -> _Change:
    return _Change(
        checkpoint_id=change.checkpoint_id,
        checkpoint_sequence=checkpoint_sequence,
        sequence=change.sequence,
        relative_path=_canonical_relative(change.relative_path),
        operation=change.operation,
        node_kind=change.node_kind,
        before_version_id=change.before_version_id,
        before_sha256=change.before_sha256,
        before_mode=change.before_mode,
        after_sha256=change.after_sha256,
        after_mode=change.after_mode,
        after_size=change.after_size,
        details=dict(change.details or {}),
    )


def _validated_todos(value: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise RewindConflictError("Checkpoint todo snapshot is invalid")
        item_id = raw.get("id")
        content = raw.get("content")
        status = raw.get("status")
        active_form = raw.get("active_form", "")
        position = raw.get("position")
        goal_id = raw.get("goal_id")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in ids
            or not isinstance(content, str)
            or not isinstance(status, str)
            or not isinstance(active_form, str)
            or type(position) is not int
            or (goal_id is not None and not isinstance(goal_id, str))
        ):
            raise RewindConflictError("Checkpoint todo snapshot is invalid")
        ids.add(item_id)
        result.append(
            {
                "id": item_id,
                "goal_id": goal_id,
                "content": content,
                "status": status,
                "active_form": active_form,
                "position": position,
            }
        )
        if len(result) > _MAX_TODOS:
            raise RewindConflictError("Checkpoint todo snapshot exceeds the rewind limit")
    return tuple(result)


def _before_state(change: _Change, store: FileVersionStore) -> _State:
    if change.operation == "created":
        return _ABSENT
    if change.node_kind == "directory":
        if change.operation != "deleted" or change.before_mode is None:
            raise RewindConflictError("Directory rewind evidence is incomplete")
        return _State(exists=True, kind="directory", mode=change.before_mode)
    if change.node_kind != "file" or change.before_version_id is None:
        raise RewindConflictError("File rewind evidence is incomplete")
    try:
        version = store.get_version(change.before_version_id)
    except FileVersionError as exc:
        raise RewindConflictError(
            f"Pinned rewind version is unavailable for {change.relative_path}"
        ) from exc
    if (
        version.relative_path != change.relative_path
        or version.sha256 != change.before_sha256
        or version.original_mode != change.before_mode
    ):
        raise RewindConflictError(
            f"Pinned rewind version does not match ledger evidence: {change.relative_path}"
        )
    return _State(
        exists=True,
        kind="file",
        mode=change.before_mode,
        sha256=version.sha256,
        size=version.size,
        version_id=version.id,
    )


def _after_state(change: _Change) -> _State:
    if change.operation == "deleted":
        return _ABSENT
    if change.after_mode is None:
        raise RewindConflictError(
            f"Checkpoint after-mode is missing: {change.relative_path}"
        )
    if change.node_kind == "directory":
        if change.after_sha256 is not None or change.after_size is not None:
            raise RewindConflictError("Directory rewind evidence is inconsistent")
        return _State(exists=True, kind="directory", mode=change.after_mode)
    if change.node_kind == "symlink":
        target = change.details.get("link_target")
        if not isinstance(target, str):
            raise RewindConflictError(
                f"Symbolic-link target is missing: {change.relative_path}"
            )
        target_bytes = target.encode("utf-8")
        if (
            change.operation != "created"
            or change.after_sha256 != hashlib.sha256(target_bytes).hexdigest()
            or change.after_size != len(target_bytes)
        ):
            raise RewindConflictError(
                f"Symbolic-link evidence is inconsistent: {change.relative_path}"
            )
        return _State(
            exists=True,
            kind="symlink",
            mode=change.after_mode,
            sha256=change.after_sha256,
            size=change.after_size,
            link_target=target,
        )
    if (
        change.node_kind != "file"
        or change.after_sha256 is None
        or change.after_size is None
    ):
        raise RewindConflictError(
            f"File after-state is incomplete: {change.relative_path}"
        )
    return _State(
        exists=True,
        kind="file",
        mode=change.after_mode,
        sha256=change.after_sha256,
        size=change.after_size,
    )


def _build_entries(
    changes: Iterable[_Change],
    store: FileVersionStore,
) -> tuple[_PlanEntry, ...]:
    grouped: dict[str, list[_Change]] = {}
    for change in sorted(
        changes,
        key=lambda item: (item.checkpoint_sequence, item.sequence, item.checkpoint_id),
    ):
        grouped.setdefault(change.relative_path, []).append(change)

    entries: list[_PlanEntry] = []
    for relative, chain in sorted(grouped.items()):
        desired = _before_state(chain[0], store)
        cursor = desired
        for change in chain:
            before = _before_state(change, store)
            if before != cursor:
                raise RewindConflictError(
                    f"Checkpoint history is discontinuous: {relative}"
                )
            cursor = _after_state(change)
        entries.append(_PlanEntry(relative, cursor, desired))

    desired_by_path = {entry.relative_path: entry.desired for entry in entries}
    for entry in entries:
        path = PurePosixPath(entry.relative_path)
        for parent in path.parents:
            if parent == PurePosixPath("."):
                break
            parent_state = desired_by_path.get(parent.as_posix())
            if parent_state is not None and not parent_state.exists and entry.desired.exists:
                raise RewindConflictError(
                    "Checkpoint history would restore a child below an absent directory"
                )
    return tuple(entries)


def _read_state(root: Path, relative: str) -> _State:
    target = root.joinpath(*PurePosixPath(relative).parts)
    # Every parent is checked without following links.  The transaction has the
    # same rule, but doing it here preserves the zero-visible-change guarantee.
    parent = root
    for component in PurePosixPath(relative).parts[:-1]:
        parent = parent / component
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return _ABSENT
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return _State(exists=True, kind="symlink" if stat.S_ISLNK(info.st_mode) else None)
    try:
        before = target.lstat()
    except FileNotFoundError:
        return _ABSENT
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISLNK(before.st_mode):
        link_target = os.readlink(target)
        after = target.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise RewindConflictError(f"Workspace path changed during preflight: {relative}")
        raw = link_target.encode("utf-8")
        return _State(
            exists=True,
            kind="symlink",
            mode=mode,
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            link_target=link_target,
        )
    if stat.S_ISDIR(before.st_mode):
        return _State(exists=True, kind="directory", mode=mode)
    if not stat.S_ISREG(before.st_mode):
        return _State(exists=True, kind=None, mode=mode)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RewindConflictError(f"Workspace path changed type: {relative}")
        while chunk := os.read(descriptor, _HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    visible = target.lstat()
    if (
        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
        or (visible.st_dev, visible.st_ino) != (after.st_dev, after.st_ino)
        or size != after.st_size
    ):
        raise RewindConflictError(f"Workspace file changed during preflight: {relative}")
    return _State(
        exists=True,
        kind="file",
        mode=mode,
        sha256=digest.hexdigest(),
        size=size,
    )


def _path_action(current: _State, desired: _State) -> Literal[
    "restore_file", "create_directory", "remove", "none"
]:
    if current == desired:
        return "none"
    if not desired.exists:
        return "remove"
    if desired.kind == "file":
        return "restore_file"
    if desired.kind == "directory" and not current.exists:
        return "create_directory"
    # Ledger constraints do not support restoring an existing symbolic link or
    # changing directory metadata in place.  Treat any such shape as conflict.
    return "none"


def _preflight_entries(
    workspace_root: str,
    entries: tuple[_PlanEntry, ...],
    *,
    against: Literal["final", "desired"] = "final",
) -> tuple[tuple[RewindPath, ...], tuple[RewindConflict, ...]]:
    root = Path(workspace_root)
    paths: list[RewindPath] = []
    conflicts: list[RewindConflict] = []
    current_by_path: dict[str, _State] = {}
    for entry in entries:
        try:
            current = _read_state(root, entry.relative_path)
        except (OSError, UnicodeError, RewindError) as exc:
            conflicts.append(RewindConflict(entry.relative_path, str(exc)))
            continue
        current_by_path[entry.relative_path] = current
        expected = entry.final if against == "final" else entry.desired
        if current != expected:
            conflicts.append(
                RewindConflict(
                    entry.relative_path,
                    "visible workspace state differs from checkpoint evidence",
                )
            )
        action = _path_action(current, entry.desired)
        if current != entry.desired and action == "none":
            conflicts.append(
                RewindConflict(
                    entry.relative_path,
                    "rewind would require an unsupported filesystem type transition",
                )
            )
        paths.append(
            RewindPath(
                relative_path=entry.relative_path,
                action=action,
                current_kind=current.kind if current.exists else None,
                desired_kind=entry.desired.kind if entry.desired.exists else None,
                source_version_id=entry.desired.version_id,
            )
        )

    # A directory created by the selected history can only be removed if every
    # visible descendant is itself ledger-owned and destined to be absent.
    desired = {entry.relative_path: entry.desired for entry in entries}
    for entry in entries:
        if entry.desired.exists or entry.final.kind != "directory":
            continue
        current = current_by_path.get(entry.relative_path)
        if current is None or current.kind != "directory":
            continue
        directory = root.joinpath(*PurePosixPath(entry.relative_path).parts)
        try:
            descendants = [
                item.relative_to(root).as_posix()
                for item in directory.rglob("*")
            ]
        except OSError as exc:
            conflicts.append(RewindConflict(entry.relative_path, str(exc)))
            continue
        for descendant in descendants:
            descendant_state = desired.get(descendant)
            if descendant_state is None or descendant_state.exists:
                conflicts.append(
                    RewindConflict(
                        descendant,
                        "directory contains content not owned by the rewind ledger",
                    )
                )
    return tuple(paths), tuple(dict.fromkeys(conflicts))


def _external_effects(checkpoints: Iterable[SessionCheckpoint]) -> tuple[dict[str, Any], ...]:
    effects: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        for effect in checkpoint.external_side_effects or []:
            if isinstance(effect, dict):
                effects.append({**dict(effect), "checkpoint_id": checkpoint.id})
        if checkpoint.has_irreversible_side_effects and not checkpoint.external_side_effects:
            effects.append(
                {
                    "checkpoint_id": checkpoint.id,
                    "source": "unknown",
                    "operation": "irreversible_external_effect",
                }
            )
    return tuple(effects)


async def _load_goal_boundary(
    db: AsyncSession,
    *,
    session_id: str,
    goal_run_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the target checkpoint's Goal pointer without trusting IDs alone."""

    goal = (
        await db.execute(
            select(SessionGoal).where(SessionGoal.session_id == session_id)
        )
    ).scalar_one_or_none()
    if goal_run_id is None:
        return (goal.id if goal is not None else None), None, None
    run = await db.get(GoalRun, goal_run_id)
    if (
        goal is None
        or run is None
        or run.goal_id != goal.id
        or goal.session_id != session_id
    ):
        raise RewindProvenanceError(
            "Checkpoint Goal run does not belong to the session Goal"
        )
    return goal.id, run.id, run.stream_id


async def _load_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    workspace_instance_id: str,
    checkpoint_id: str,
) -> tuple[_RewindPlan | None, RewindResult | None]:
    async with session_factory() as db:
        session = await db.get(Session, session_id)
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
        target = await db.get(SessionCheckpoint, checkpoint_id)
        if session is None or instance is None or target is None:
            raise RewindNotFoundError("Session, workspace instance, or checkpoint not found")
        if (
            target.session_id != session.id
            or target.workspace_instance_id != instance.id
        ):
            raise RewindProvenanceError(
                "Checkpoint does not belong to the requested session/workspace instance"
            )
        if instance.status != "active":
            raise RewindProvenanceError("Workspace instance is not active")
        try:
            canonical, identity = inspect_workspace_identity(instance.root_path)
        except Exception as exc:
            raise RewindProvenanceError("Workspace instance is unavailable") from exc
        if canonical != instance.root_path or identity != instance.identity_token:
            raise RewindProvenanceError("Workspace filesystem identity has changed")

        if target.state == "rewound":
            return None, _result_from_checkpoint(target)
        if target.state not in _REWINDABLE_CHECKPOINT_STATES or target.pin_state != "pinned":
            raise RewindConflictError(
                f"Checkpoint is not rewindable ({target.state}/{target.pin_state})"
            )
        if not target.anchor_message_id:
            raise RewindConflictError("Checkpoint has no conversation anchor")
        anchor = await db.get(Message, target.anchor_message_id)
        if anchor is None or anchor.session_id != session.id:
            raise RewindProvenanceError("Checkpoint conversation anchor is missing or foreign")

        later = list(
            (
                await db.execute(
                    select(SessionCheckpoint)
                    .where(
                        SessionCheckpoint.session_id == session.id,
                        SessionCheckpoint.sequence >= target.sequence,
                    )
                    .order_by(SessionCheckpoint.sequence, SessionCheckpoint.id)
                )
            ).scalars()
        )
        cross_workspace = [
            item
            for item in later
            if item.workspace_instance_id != instance.id
            and item.state in {"finalized", "rewinding"}
        ]
        if cross_workspace:
            raise RewindConflictError(
                "Conversation history crosses another workspace after the target checkpoint"
            )
        affected = [
            item for item in later if item.workspace_instance_id == instance.id
        ]
        if not affected or affected[0].id != target.id:
            raise RewindConflictError("Checkpoint ordering is inconsistent")
        if len(affected) > _MAX_AFFECTED_CHECKPOINTS:
            raise RewindConflictError("Rewind affects too many checkpoints")
        for item in affected:
            if item.state != "finalized" or item.pin_state != "pinned":
                raise RewindConflictError(
                    "Target and every later workspace checkpoint must be finalized and pinned"
                )

        sequence_by_id = {item.id: item.sequence for item in affected}
        changes = list(
            (
                await db.execute(
                    select(CheckpointChange)
                    .where(CheckpointChange.checkpoint_id.in_(sequence_by_id))
                    .order_by(
                        CheckpointChange.checkpoint_id,
                        CheckpointChange.sequence,
                    )
                )
            ).scalars()
        )
        if len(changes) > _MAX_LEDGER_CHANGES:
            raise RewindConflictError("Rewind ledger exceeds the mutation limit")
        copied_changes = tuple(
            _copy_change(item, sequence_by_id[item.checkpoint_id]) for item in changes
        )
        affected_turn_ids = {
            turn_id
            for item in affected
            for turn_id in (item.turn_run_id, *(item.child_turn_ids or []))
        }
        turns = list(
            (
                await db.execute(
                    select(TurnRun).where(TurnRun.id.in_(affected_turn_ids))
                )
            ).scalars()
        )
        if len(turns) != len(affected_turn_ids) or any(
            turn.session_id != session.id
            or turn.workspace_instance_id != instance.id
            or turn.status not in {"completed", "failed", "cancelled"}
            for turn in turns
        ):
            raise RewindProvenanceError("Affected turn provenance is inconsistent")
        todos = _validated_todos(target.todo_snapshot or [])
        goal_id, goal_run_id, goal_stream_id = await _load_goal_boundary(
            db,
            session_id=session.id,
            goal_run_id=target.goal_run_id,
        )
        todo_goal_ids = {item["goal_id"] for item in todos if item["goal_id"] is not None}
        if todo_goal_ids:
            matching_goals = set(
                (
                    await db.execute(
                        select(SessionGoal.id).where(
                            SessionGoal.session_id == session.id,
                            SessionGoal.id.in_(todo_goal_ids),
                        )
                    )
                ).scalars()
            )
            if matching_goals != todo_goal_ids:
                raise RewindProvenanceError(
                    "Checkpoint todo snapshot references a missing or foreign Goal"
                )
        todo_ids = {item["id"] for item in todos}
        if todo_ids:
            foreign_todo = (
                await db.execute(
                    select(Todo.id)
                    .where(Todo.id.in_(todo_ids), Todo.session_id != session.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if foreign_todo is not None:
                raise RewindProvenanceError(
                    "Checkpoint todo identity is now owned by another session"
                )
        effects = _external_effects(affected)
        target_turn_run_id = target.turn_run_id
        target_root_turn_id = target.root_turn_id
        target_anchor = target.anchor_message_id
        checkpoint_ids = tuple(item.id for item in affected)
        workspace_root = instance.root_path

    store = FileVersionStore(
        workspace_root,
        expected_durable_workspace_identity=identity,
    )
    entries = await asyncio.to_thread(_build_entries, copied_changes, store)
    return (
        _RewindPlan(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            workspace_identity_token=identity,
            workspace_root=workspace_root,
            target_checkpoint_id=checkpoint_id,
            target_turn_run_id=target_turn_run_id,
            target_root_turn_id=target_root_turn_id,
            anchor_message_id=target_anchor,
            affected_checkpoint_ids=checkpoint_ids,
            affected_turn_ids=tuple(sorted(affected_turn_ids)),
            todo_snapshot=todos,
            goal_id=goal_id,
            goal_run_id=goal_run_id,
            goal_stream_id=goal_stream_id,
            entries=entries,
            external_side_effects=effects,
        ),
        None,
    )


def _result_from_checkpoint(checkpoint: SessionCheckpoint) -> RewindResult:
    raw = dict(checkpoint.details or {}).get("rewind_result")
    if not isinstance(raw, dict):
        raise RewindConflictError("Rewound checkpoint is missing its durable result")
    try:
        return RewindResult(
            session_id=checkpoint.session_id,
            workspace_instance_id=checkpoint.workspace_instance_id,
            target_checkpoint_id=checkpoint.id,
            affected_checkpoint_ids=tuple(str(item) for item in raw["affected_checkpoint_ids"]),
            changed_paths=tuple(str(item) for item in raw["changed_paths"]),
            messages_removed=int(raw["messages_removed"]),
            todos_restored=int(raw["todos_restored"]),
            external_side_effects=tuple(
                dict(item) for item in raw.get("external_side_effects", [])
            ),
            already_rewound=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RewindConflictError("Durable rewind result is invalid") from exc


async def _quiescence_blockers(
    session_factory: async_sessionmaker[AsyncSession],
    stream_manager: _StreamManager | None,
    plan: _RewindPlan,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if stream_manager is None:
        blockers.append("stream manager quiescence guard is unavailable")
    elif stream_manager.active_job_for_session(plan.session_id) is not None:
        blockers.append("session has an active generation job")
    async with session_factory() as db:
        running_turn = (
            await db.execute(
                select(TurnRun.id)
                .where(
                    TurnRun.status == "running",
                    or_(
                        TurnRun.session_id == plan.session_id,
                        TurnRun.workspace_instance_id == plan.workspace_instance_id,
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if running_turn is not None:
            blockers.append("database has a running turn for the session/workspace")
        goal = (
            await db.execute(
                select(SessionGoal).where(SessionGoal.session_id == plan.session_id)
            )
        ).scalar_one_or_none()
        if goal is not None and (goal.run_state != "idle" or goal.status == "active"):
            blockers.append("session Goal must be idle and non-active")
        active_goal_run = (
            await db.execute(
                select(GoalRun.id)
                .join(SessionGoal, SessionGoal.id == GoalRun.goal_id)
                .where(
                    SessionGoal.session_id == plan.session_id,
                    GoalRun.status.in_(_ACTIVE_GOAL_RUN_STATES),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if active_goal_run is not None:
            blockers.append("session has an active Goal run")
    return tuple(blockers)


async def _mark_rewinding(
    session_factory: async_sessionmaker[AsyncSession],
    plan: _RewindPlan,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            result = await db.execute(
                update(SessionCheckpoint)
                .where(
                    SessionCheckpoint.id.in_(plan.affected_checkpoint_ids),
                    SessionCheckpoint.session_id == plan.session_id,
                    SessionCheckpoint.workspace_instance_id == plan.workspace_instance_id,
                    SessionCheckpoint.state == "finalized",
                    SessionCheckpoint.pin_state == "pinned",
                )
                .values(state="rewinding")
            )
            if result.rowcount != len(plan.affected_checkpoint_ids):
                raise RewindConflictError("Checkpoint state changed during rewind admission")


async def _compensate_rewinding(
    session_factory: async_sessionmaker[AsyncSession],
    checkpoint_ids: Iterable[str],
) -> None:
    async with session_factory() as db:
        async with db.begin():
            for checkpoint_id in checkpoint_ids:
                checkpoint = await db.get(SessionCheckpoint, checkpoint_id)
                if checkpoint is not None and checkpoint.state == "rewinding":
                    await transition_checkpoint(db, checkpoint.id, target_state="finalized")


def _apply_files(plan: _RewindPlan) -> tuple[tuple[str, ...], str | None]:
    changed_entries = tuple(
        entry for entry in plan.entries if entry.final != entry.desired
    )
    if not changed_entries:
        return (), None
    if sys.platform == "win32":
        # Full-workspace staging is required for directory and multi-file
        # atomicity.  The current Windows transaction implementation cannot
        # provide that boundary, so v1.1 fails closed instead of overclaiming.
        raise RewindConflictError(
            "Atomic session rewind is unavailable on this Windows transaction runtime"
        )
    ctx = ToolContext(
        session_id=plan.session_id,
        message_id=plan.anchor_message_id,
        agent=AgentInfo(
            name="rewind",
            description="server-owned checkpoint rewind",
            mode="hidden",
        ),
        call_id=f"rewind:{plan.target_checkpoint_id}",
        workspace=plan.workspace_root,
        root_turn_id=plan.target_root_turn_id,
        turn_run_id=plan.target_turn_run_id,
        checkpoint_id=plan.target_checkpoint_id,
        workspace_instance_id=plan.workspace_instance_id,
        workspace_identity_token=plan.workspace_identity_token,
    )
    transaction = WorkspaceMutationTransaction(
        plan.workspace_root,
        ctx,
        operation="checkpoint_rewind",
        checkpoint_action="rewind",
        rewind_checkpoint_ids=plan.affected_checkpoint_ids,
    )
    try:
        file_only = all(
            entry.final.kind in {None, "file"}
            and entry.desired.kind in {None, "file"}
            for entry in changed_entries
        )
        staged = (
            transaction.prepare_paths(
                entry.relative_path for entry in changed_entries
            )
            if file_only
            else transaction.prepare()
        )
        # The first service-level preflight happens before the transaction is
        # prepared.  An out-of-band writer could otherwise land a new state in
        # that gap and have it accepted as the transaction baseline.  Recheck
        # the original ledger final-state now, before touching the private
        # stage; later writers are caught by commit's immutable baseline proof.
        _paths, preparation_conflicts = _preflight_entries(
            plan.workspace_root,
            plan.entries,
        )
        if preparation_conflicts:
            raise RewindConflictError(
                "Workspace changed while rewind staging was prepared",
                conflicts=preparation_conflicts,
            )
        store = FileVersionStore(
            plan.workspace_root,
            expected_durable_workspace_identity=plan.workspace_identity_token,
        )
        for entry in sorted(
            (
                item
                for item in changed_entries
                if item.desired.kind == "directory" and item.desired.exists
            ),
            key=lambda item: (item.relative_path.count("/"), item.relative_path),
        ):
            target = staged.joinpath(*PurePosixPath(entry.relative_path).parts)
            target.mkdir(mode=entry.desired.mode or 0o700, exist_ok=True)
            os.chmod(target, entry.desired.mode or 0o700)
        for entry in changed_entries:
            if entry.desired.kind != "file" or not entry.desired.exists:
                continue
            if entry.desired.version_id is None:
                raise RewindConflictError(
                    f"File restore source is unavailable: {entry.relative_path}"
                )
            store.materialize_version_in_transaction(
                entry.desired.version_id,
                staged,
                expected_relative_path=entry.relative_path,
            )
        for entry in sorted(
            (item for item in changed_entries if not item.desired.exists),
            key=lambda item: (-item.relative_path.count("/"), item.relative_path),
        ):
            target = staged.joinpath(*PurePosixPath(entry.relative_path).parts)
            try:
                info = target.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                target.rmdir()
            else:
                target.unlink()
        commit = transaction.commit()
        token = commit.checkpoint_journal_token
        return tuple(item.relative_path for item in changed_entries), token
    except Exception:
        transaction.abort()
        raise


async def _complete_database(
    session_factory: async_sessionmaker[AsyncSession],
    plan: _RewindPlan,
    *,
    changed_paths: tuple[str, ...],
) -> RewindResult:
    completed_at = _now()
    changed_path_set = frozenset(changed_paths)
    async with session_factory() as db:
        async with db.begin():
            checkpoints = list(
                (
                    await db.execute(
                        select(SessionCheckpoint)
                        .where(SessionCheckpoint.id.in_(plan.affected_checkpoint_ids))
                        .order_by(SessionCheckpoint.sequence)
                    )
                ).scalars()
            )
            if len(checkpoints) != len(plan.affected_checkpoint_ids):
                raise RewindProvenanceError("Affected checkpoint disappeared during rewind")
            if all(item.state == "rewound" for item in checkpoints):
                target = next(
                    item for item in checkpoints if item.id == plan.target_checkpoint_id
                )
                return _result_from_checkpoint(target)
            if any(
                item.state != "rewinding"
                or item.pin_state != "pinned"
                or item.session_id != plan.session_id
                or item.workspace_instance_id != plan.workspace_instance_id
                for item in checkpoints
            ):
                raise RewindConflictError("Checkpoint rewind intent changed before completion")

            messages = list(
                (
                    await db.execute(
                        select(Message)
                        .where(Message.session_id == plan.session_id)
                        .order_by(Message.time_created, Message.id)
                    )
                ).scalars()
            )
            anchor_index = next(
                (index for index, message in enumerate(messages) if message.id == plan.anchor_message_id),
                None,
            )
            if anchor_index is None:
                raise RewindProvenanceError("Conversation anchor disappeared before completion")
            removed = messages[anchor_index:]
            await invalidate_acp_prompt_ledgers_for_messages(
                db,
                session_id=plan.session_id,
                messages=removed,
            )
            for message in removed:
                await db.delete(message)

            await db.execute(delete(Todo).where(Todo.session_id == plan.session_id))
            for raw in plan.todo_snapshot:
                db.add(Todo(session_id=plan.session_id, **dict(raw)))

            current_goal = (
                await db.execute(
                    select(SessionGoal).where(SessionGoal.session_id == plan.session_id)
                )
            ).scalar_one_or_none()
            if plan.goal_id is None:
                if current_goal is not None:
                    raise RewindProvenanceError(
                        "A session Goal appeared after rewind admission"
                    )
            else:
                if current_goal is None or current_goal.id != plan.goal_id:
                    raise RewindProvenanceError(
                        "Target session Goal disappeared or changed identity"
                    )
                boundary_run: GoalRun | None = None
                if plan.goal_run_id is not None:
                    boundary_run = await db.get(GoalRun, plan.goal_run_id)
                    if (
                        boundary_run is None
                        or boundary_run.goal_id != current_goal.id
                        or boundary_run.stream_id != plan.goal_stream_id
                    ):
                        raise RewindProvenanceError(
                            "Target Goal run disappeared or changed provenance"
                        )
                current_goal.status = "paused"
                current_goal.run_state = "idle"
                current_goal.last_run_id = plan.goal_run_id
                current_goal.last_stream_id = (
                    boundary_run.stream_id if boundary_run is not None else None
                )
                current_goal.revision = int(current_goal.revision) + 1
                current_goal.blocker_code = None
                current_goal.blocker_message = None
                current_goal.blocker_streak = 0
                current_goal.needs_review = False
                current_goal.next_retry_at = None
                current_goal.no_progress_count = 0
                current_goal.consecutive_error_count = 0
                current_goal.completion_summary = None
                current_goal.completion_evidence = None
                current_goal.time_completed = None

            turns = list(
                (
                    await db.execute(
                        select(TurnRun).where(TurnRun.id.in_(plan.affected_turn_ids))
                    )
                ).scalars()
            )
            if len(turns) != len(plan.affected_turn_ids):
                raise RewindProvenanceError("Affected turn disappeared during rewind")
            for turn in turns:
                if turn.status not in {"completed", "failed", "cancelled", "rewound"}:
                    raise RewindConflictError("Affected turn became active during rewind")
                turn.status = "rewound"

            durable_result = {
                "affected_checkpoint_ids": list(plan.affected_checkpoint_ids),
                "changed_paths": list(changed_paths),
                "restored_paths": [
                    {
                        "relative_path": entry.relative_path,
                        "exists": entry.desired.exists,
                        "node_kind": entry.desired.kind,
                        "sha256": entry.desired.sha256,
                        "mode": entry.desired.mode,
                        "size": entry.desired.size,
                    }
                    for entry in plan.entries
                    if entry.relative_path in changed_path_set
                ],
                "messages_removed": len(removed),
                "todos_restored": len(plan.todo_snapshot),
                "external_side_effects": [dict(item) for item in plan.external_side_effects],
                "time_completed": completed_at.isoformat(),
            }
            for checkpoint in checkpoints:
                details = dict(checkpoint.details or {})
                details["rewound_by_checkpoint_id"] = plan.target_checkpoint_id
                if checkpoint.id == plan.target_checkpoint_id:
                    details["rewind_result"] = durable_result
                checkpoint.details = details
                checkpoint.state = "rewound"
                checkpoint.time_rewound = completed_at
                checkpoint.pin_state = "released"
                checkpoint.time_pin_released = completed_at

    # DB is now the source of truth.  Manifest cleanup is idempotent and a
    # startup pin reconciliation can finish it after an abrupt exit.
    store = FileVersionStore(
        plan.workspace_root,
        expected_durable_workspace_identity=plan.workspace_identity_token,
    )
    for checkpoint_id in plan.affected_checkpoint_ids:
        try:
            store.unpin_versions(f"checkpoint:{checkpoint_id}")
        except Exception:
            # Retaining an object is safe.  The released DB pin remains the
            # source of truth and startup reconciliation will retry cleanup.
            logger.warning(
                "Could not release file-version owner for rewound checkpoint %s",
                checkpoint_id,
                exc_info=True,
            )
    return RewindResult(
        session_id=plan.session_id,
        workspace_instance_id=plan.workspace_instance_id,
        target_checkpoint_id=plan.target_checkpoint_id,
        affected_checkpoint_ids=plan.affected_checkpoint_ids,
        changed_paths=changed_paths,
        messages_removed=len(removed),
        todos_restored=len(plan.todo_snapshot),
        external_side_effects=plan.external_side_effects,
    )


@asynccontextmanager
async def _admission_guard(
    stream_manager: _StreamManager | None,
) -> AsyncIterator[None]:
    if stream_manager is None or not hasattr(stream_manager, "job_admission_lock"):
        raise RewindBusyError("Stream admission guard is unavailable")
    async with stream_manager.job_admission_lock:
        yield


class RewindService:
    """List, preview, and execute strict session/workspace rewinds."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        stream_manager: _StreamManager | None,
        enabled: bool | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.stream_manager = stream_manager
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return rewind_runtime_enabled() if self._enabled is None else self._enabled

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RewindDisabledError("v1.1 rewind is not released")

    async def list(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        limit: int = 100,
    ) -> tuple[RewindCheckpointItem, ...]:
        self._require_enabled()
        if limit < 1 or limit > 500:
            raise RewindConflictError("limit must be between 1 and 500")
        async with self.session_factory() as db:
            session = await db.get(Session, session_id)
            instance = await db.get(WorkspaceInstance, workspace_instance_id)
            if session is None or instance is None:
                raise RewindNotFoundError("Session or workspace instance not found")
            rows = list(
                (
                    await db.execute(
                        select(SessionCheckpoint)
                        .where(
                            SessionCheckpoint.session_id == session.id,
                            SessionCheckpoint.workspace_instance_id == instance.id,
                            SessionCheckpoint.state.in_(("finalized", "rewinding", "rewound")),
                        )
                        .order_by(SessionCheckpoint.sequence.desc())
                        .limit(limit)
                    )
                ).scalars()
            )
        return tuple(
            RewindCheckpointItem(
                checkpoint_id=item.id,
                sequence=item.sequence,
                state=item.state,
                pin_state=item.pin_state,
                anchor_message_id=item.anchor_message_id,
                turn_run_id=item.turn_run_id,
                has_irreversible_side_effects=item.has_irreversible_side_effects,
                external_side_effects=tuple(
                    dict(effect)
                    for effect in (item.external_side_effects or [])
                    if isinstance(effect, dict)
                ),
            )
            for item in rows
        )

    async def list_checkpoints(self, **kwargs: Any) -> tuple[RewindCheckpointItem, ...]:
        return await self.list(**kwargs)

    async def preview(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        checkpoint_id: str,
    ) -> RewindPreview:
        self._require_enabled()
        plan, replay = await _load_plan(
            self.session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            checkpoint_id=checkpoint_id,
        )
        if replay is not None:
            return RewindPreview(
                session_id=replay.session_id,
                workspace_instance_id=replay.workspace_instance_id,
                target_checkpoint_id=replay.target_checkpoint_id,
                affected_checkpoint_ids=replay.affected_checkpoint_ids,
                paths=(),
                conflicts=(),
                blockers=(),
                external_side_effects=replay.external_side_effects,
                already_rewound=True,
            )
        assert plan is not None
        paths, conflicts = await asyncio.to_thread(
            _preflight_entries,
            plan.workspace_root,
            plan.entries,
        )
        blockers = await _quiescence_blockers(
            self.session_factory, self.stream_manager, plan
        )
        return RewindPreview(
            session_id=plan.session_id,
            workspace_instance_id=plan.workspace_instance_id,
            target_checkpoint_id=plan.target_checkpoint_id,
            affected_checkpoint_ids=plan.affected_checkpoint_ids,
            paths=paths,
            conflicts=conflicts,
            blockers=blockers,
            external_side_effects=plan.external_side_effects,
        )

    async def execute(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        checkpoint_id: str,
    ) -> RewindResult:
        self._require_enabled()
        async with _admission_guard(self.stream_manager):
            plan, replay = await _load_plan(
                self.session_factory,
                session_id=session_id,
                workspace_instance_id=workspace_instance_id,
                checkpoint_id=checkpoint_id,
            )
            if replay is not None:
                return replay
            assert plan is not None
            _paths, conflicts = await asyncio.to_thread(
                _preflight_entries,
                plan.workspace_root,
                plan.entries,
            )
            if conflicts:
                raise RewindConflictError(
                    "Workspace conflicts with the checkpoint ledger",
                    conflicts=conflicts,
                )
            blockers = await _quiescence_blockers(
                self.session_factory, self.stream_manager, plan
            )
            if blockers:
                raise RewindBusyError("; ".join(blockers))
            await _mark_rewinding(self.session_factory, plan)
            token: str | None = None
            try:
                changed_paths, token = await asyncio.to_thread(_apply_files, plan)
                result = await _complete_database(
                    self.session_factory,
                    plan,
                    changed_paths=changed_paths,
                )
            except Exception:
                # If a durable commit exists, compensating the DB intent would
                # lie about the visible files.  Leave it for journal recovery.
                committed = await asyncio.to_thread(list_committed_checkpoint_journals)
                owns_committed_journal = False
                for _journal_token, payload in committed:
                    try:
                        action, checkpoint_ids = committed_checkpoint_journal_action(payload)
                    except WorkspaceMutationError:
                        continue
                    if action == "rewind" and plan.target_checkpoint_id in checkpoint_ids:
                        owns_committed_journal = True
                        break
                if not owns_committed_journal:
                    await _compensate_rewinding(
                        self.session_factory, plan.affected_checkpoint_ids
                    )
                raise
            if token is not None:
                try:
                    await asyncio.to_thread(
                        cleanup_committed_checkpoint_journal,
                        token,
                        expected_checkpoint_id=plan.target_checkpoint_id,
                    )
                except Exception:
                    # Result is already durable and idempotent; startup will
                    # validate and clean the retained bridge.
                    pass
            return result


async def recover_committed_rewind_journal(
    session_factory: async_sessionmaker[AsyncSession],
    token: str,
    payload: dict[str, object],
) -> bool:
    """Finish one committed rewind journal during startup recovery.

    Returns ``True`` when database state was completed, and ``False`` when an
    earlier attempt had already completed it.  Both paths clean the journal.
    """

    action, checkpoint_ids = committed_checkpoint_journal_action(payload)
    if action != "rewind":
        raise RewindConflictError("Journal is not a rewind action")
    raw_runtime = payload.get("runtime_checkpoint")
    if not isinstance(raw_runtime, dict):
        raise RewindProvenanceError("Rewind journal runtime identity is invalid")
    required = (
        "session_id",
        "checkpoint_id",
        "workspace_instance_id",
        "root_turn_id",
        "turn_run_id",
    )
    if not all(isinstance(raw_runtime.get(key), str) and raw_runtime[key] for key in required):
        raise RewindProvenanceError("Rewind journal runtime identity is incomplete")
    target_id = str(raw_runtime["checkpoint_id"])
    session_id = str(raw_runtime["session_id"])
    workspace_id = str(raw_runtime["workspace_instance_id"])

    async with session_factory() as db:
        target = await db.get(SessionCheckpoint, target_id)
        instance = await db.get(WorkspaceInstance, workspace_id)
        checkpoints = list(
            (
                await db.execute(
                    select(SessionCheckpoint).where(SessionCheckpoint.id.in_(checkpoint_ids))
                )
            ).scalars()
        )
    if target is None or instance is None or len(checkpoints) != len(checkpoint_ids):
        raise RewindProvenanceError("Committed rewind journal has no complete database owner")
    if (
        target.session_id != session_id
        or target.workspace_instance_id != workspace_id
        or target.root_turn_id != raw_runtime["root_turn_id"]
        or target.turn_run_id != raw_runtime["turn_run_id"]
        or payload.get("workspace") != instance.root_path
        or any(
            item.session_id != session_id or item.workspace_instance_id != workspace_id
            for item in checkpoints
        )
    ):
        raise RewindProvenanceError("Committed rewind journal provenance does not match")
    if target.state == "rewound":
        _result_from_checkpoint(target)
        await asyncio.to_thread(
            cleanup_committed_checkpoint_journal,
            token,
            expected_checkpoint_id=target_id,
        )
        return False
    if any(item.state != "rewinding" or item.pin_state != "pinned" for item in checkpoints):
        raise RewindConflictError("Committed rewind intent is not recoverable")

    # Rebuild the same immutable plan from the marked checkpoint set without
    # requiring the ordinary finalized-only admission path.
    plan = await _load_marked_plan(
        session_factory,
        target_id=target_id,
        checkpoint_ids=checkpoint_ids,
        session_id=session_id,
        workspace_instance_id=workspace_id,
    )
    _paths, conflicts = await asyncio.to_thread(
        _preflight_entries,
        plan.workspace_root,
        plan.entries,
        against="desired",
    )
    if conflicts:
        raise RewindConflictError(
            "Committed rewind files do not match the intended restored state",
            conflicts=conflicts,
        )
    changed_paths = tuple(
        entry.relative_path for entry in plan.entries if entry.final != entry.desired
    )
    await _complete_database(session_factory, plan, changed_paths=changed_paths)
    await asyncio.to_thread(
        cleanup_committed_checkpoint_journal,
        token,
        expected_checkpoint_id=target_id,
    )
    return True


async def _load_marked_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_id: str,
    checkpoint_ids: tuple[str, ...],
    session_id: str,
    workspace_instance_id: str,
) -> _RewindPlan:
    async with session_factory() as db:
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
        target = await db.get(SessionCheckpoint, target_id)
        checkpoints = list(
            (
                await db.execute(
                    select(SessionCheckpoint)
                    .where(SessionCheckpoint.id.in_(checkpoint_ids))
                    .order_by(SessionCheckpoint.sequence)
                )
            ).scalars()
        )
        if instance is None or target is None or len(checkpoints) != len(checkpoint_ids):
            raise RewindProvenanceError("Marked rewind ledger is incomplete")
        if tuple(item.id for item in checkpoints) != checkpoint_ids:
            raise RewindProvenanceError("Rewind journal checkpoint order is inconsistent")
        sequence_by_id = {item.id: item.sequence for item in checkpoints}
        changes = list(
            (
                await db.execute(
                    select(CheckpointChange).where(
                        CheckpointChange.checkpoint_id.in_(checkpoint_ids)
                    )
                )
            ).scalars()
        )
        affected_turn_ids = {
            turn_id
            for item in checkpoints
            for turn_id in (item.turn_run_id, *(item.child_turn_ids or []))
        }
        copied = tuple(_copy_change(item, sequence_by_id[item.checkpoint_id]) for item in changes)
        todos = _validated_todos(target.todo_snapshot or [])
        goal_id, goal_run_id, goal_stream_id = await _load_goal_boundary(
            db,
            session_id=session_id,
            goal_run_id=target.goal_run_id,
        )
        effects = _external_effects(checkpoints)
        workspace_root = instance.root_path
        anchor = target.anchor_message_id
        if not anchor:
            raise RewindProvenanceError("Marked rewind target has no conversation anchor")
        canonical, identity = inspect_workspace_identity(workspace_root)
        if canonical != workspace_root or identity != instance.identity_token:
            raise RewindProvenanceError("Marked rewind workspace identity changed")
    store = FileVersionStore(
        workspace_root,
        expected_durable_workspace_identity=identity,
    )
    entries = await asyncio.to_thread(_build_entries, copied, store)
    return _RewindPlan(
        session_id=session_id,
        workspace_instance_id=workspace_instance_id,
        workspace_identity_token=identity,
        workspace_root=workspace_root,
        target_checkpoint_id=target_id,
        target_turn_run_id=target.turn_run_id,
        target_root_turn_id=target.root_turn_id,
        anchor_message_id=anchor,
        affected_checkpoint_ids=checkpoint_ids,
        affected_turn_ids=tuple(sorted(affected_turn_ids)),
        todo_snapshot=todos,
        goal_id=goal_id,
        goal_run_id=goal_run_id,
        goal_stream_id=goal_stream_id,
        entries=entries,
        external_side_effects=effects,
    )


async def recover_stale_rewind_intents(
    session_factory: async_sessionmaker[AsyncSession],
    committed_checkpoint_ids: Iterable[str],
) -> int:
    """Compensate pre-commit rewind intents after transaction recovery.

    The caller supplies every checkpoint ID owned by a retained committed
    rewind journal.  Only other ``rewinding`` rows are returned to finalized.
    """

    committed = {str(value) for value in committed_checkpoint_ids}
    recovered = 0
    async with session_factory() as db:
        async with db.begin():
            stale = list(
                (
                    await db.execute(
                        select(SessionCheckpoint).where(
                            SessionCheckpoint.state == "rewinding"
                        )
                    )
                ).scalars()
            )
            for checkpoint in stale:
                if checkpoint.id in committed:
                    continue
                await transition_checkpoint(db, checkpoint.id, target_state="finalized")
                recovered += 1
    return recovered


__all__ = [
    "RewindBusyError",
    "RewindCheckpointItem",
    "RewindConflict",
    "RewindConflictError",
    "RewindDisabledError",
    "RewindError",
    "RewindNotFoundError",
    "RewindPath",
    "RewindPreview",
    "RewindProvenanceError",
    "RewindResult",
    "RewindService",
    "recover_committed_rewind_journal",
    "recover_stale_rewind_intents",
    "rewind_runtime_enabled",
]
