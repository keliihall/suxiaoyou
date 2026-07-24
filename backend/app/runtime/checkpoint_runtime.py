"""Runtime integration for durable v1.1 turn checkpoints.

The storage layer deliberately owns no commits.  These helpers define the
SessionPrompt transaction boundaries and keep legacy v1.0 behaviour untouched
while the release gate is closed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import stat
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.checkpoint_change import CheckpointChange
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.turn_run import TurnRun
from app.models.workspace_instance import WorkspaceInstance
from app.storage.checkpoints import (
    CheckpointConflictError,
    CheckpointLedgerError,
    create_child_turn,
    create_root_turn,
    finish_turn,
    prepare_checkpoint,
    record_checkpoint_change,
    record_irreversible_side_effect,
    reconcile_workspace_checkpoint_pins,
    register_workspace_instance,
    release_checkpoint_pin,
    transition_checkpoint,
)
from app.storage.file_versions import FileVersionError

if TYPE_CHECKING:
    from app.streaming.manager import GenerationJob
    from app.tool.workspace_transaction import WorkspaceTransactionRecoveryReport


TurnFinishStatus = Literal["completed", "failed", "cancelled"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnCheckpointBinding:
    root_turn_id: str
    turn_run_id: str
    checkpoint_id: str
    workspace_instance_id: str
    workspace_root: str


def checkpoint_runtime_enabled() -> bool:
    """Read the code-owned gate dynamically so tests and rollout can override it."""

    from app import release_features

    return bool(release_features.V11_CHECKPOINTS_RELEASED)


async def admit_turn_checkpoint(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job: GenerationJob,
    workspace: str,
    request_message_id: str | None,
    todo_snapshot: list[dict[str, Any]],
    workspace_kind: str = "direct",
) -> TurnCheckpointBinding | None:
    """Create and prepare the owner checkpoint before any tool can execute."""

    if not checkpoint_runtime_enabled():
        return None

    workspace_root = str(Path(workspace).resolve(strict=True))
    created_turn = False
    prepared_now = False
    async with session_factory() as db:
        async with db.begin():
            session = await db.get(Session, job.session_id)
            if session is None:
                raise CheckpointConflictError("Session disappeared before turn admission")
            instance = await register_workspace_instance(
                db,
                workspace_root,
                kind=workspace_kind,
                project_id=session.project_id,
                created_by_session_id=session.id,
                details={"managed": workspace_kind == "managed"},
            )
            if (
                job.workspace_instance_id is not None
                and job.workspace_instance_id != instance.id
            ):
                raise CheckpointConflictError(
                    "Generation job is bound to a different workspace instance"
                )

            existing_turn = await db.get(TurnRun, job.turn_run_id)
            if job.parent_turn_id is None:
                turn = await create_root_turn(
                    db,
                    session_id=session.id,
                    workspace_instance_id=instance.id,
                    source_kind=job.invocation_source,
                    turn_id=job.turn_run_id,
                    request_message_id=request_message_id,
                    stream_id=job.stream_id,
                    details={"invocation_source_id": job.invocation_source_id},
                )
            else:
                turn = await create_child_turn(
                    db,
                    parent_turn_id=job.parent_turn_id,
                    session_id=session.id,
                    workspace_instance_id=instance.id,
                    source_kind="subagent",
                    turn_id=job.turn_run_id,
                    request_message_id=request_message_id,
                    stream_id=job.stream_id,
                    details={"invocation_source": job.invocation_source},
                )
            created_turn = existing_turn is None

            existing_checkpoint = (
                await db.execute(
                    select(SessionCheckpoint).where(
                        SessionCheckpoint.turn_run_id == turn.id
                    )
                )
            ).scalar_one_or_none()
            checkpoint = await prepare_checkpoint(
                db,
                turn_run_id=turn.id,
                anchor_message_id=request_message_id,
                goal_run_id=job.goal_run_id,
                todo_snapshot=todo_snapshot,
                details={"stream_id": job.stream_id},
            )
            if checkpoint.state == "prepared":
                await transition_checkpoint(
                    db,
                    checkpoint.id,
                    target_state="committing",
                )
                prepared_now = True
            elif checkpoint.state != "committing":
                raise CheckpointConflictError(
                    f"Turn checkpoint is already {checkpoint.state}"
                )
            prepared_now = prepared_now or existing_checkpoint is None

            instance_id = instance.id
            turn_id = turn.id
            root_turn_id = turn.root_turn_id
            checkpoint_id = checkpoint.id
            source_kind = turn.source_kind

    job.bind_workspace_instance(instance_id)
    binding = TurnCheckpointBinding(
        root_turn_id=root_turn_id,
        turn_run_id=turn_id,
        checkpoint_id=checkpoint_id,
        workspace_instance_id=instance_id,
        workspace_root=workspace_root,
    )
    if created_turn:
        job.publish_lifecycle(
            "turn.started",
            {"source_kind": source_kind},
            message_id=request_message_id,
            checkpoint_id=checkpoint_id,
        )
    if prepared_now:
        job.publish_lifecycle(
            "checkpoint.prepared",
            {"state": "committing"},
            message_id=request_message_id,
            checkpoint_id=checkpoint_id,
        )
    return binding


def _same_change(existing: CheckpointChange, mutation: dict[str, Any]) -> bool:
    return all(
        getattr(existing, field) == mutation.get(field)
        for field in (
            "operation",
            "node_kind",
            "relative_path",
            "before_version_id",
            "before_sha256",
            "before_mode",
            "after_sha256",
            "after_mode",
            "after_size",
        )
    )


def _metadata_paths(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise CheckpointConflictError(f"Tool returned invalid {field} metadata")
    return list(value)


def _validated_office_report(
    value: object,
    *,
    binding: TurnCheckpointBinding,
    mutations: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    """Bind one private Office report to its exact committed file mutation."""

    if value is None:
        return None
    from app.office_validation import (
        OfficeValidationContractError,
        OfficeValidationReport,
    )

    try:
        report = OfficeValidationReport.from_dict(value)
    except OfficeValidationContractError as exc:
        raise CheckpointConflictError(
            "Office validation evidence has an invalid contract"
        ) from exc
    if (
        report.verdict != "pass"
        or report.checkpoint_id != binding.checkpoint_id
        or report.root_turn_id != binding.root_turn_id
    ):
        raise CheckpointConflictError(
            "Office validation evidence does not belong to this checkpoint"
        )
    authoritative = tuple(
        check
        for check in report.checks
        if check.code == "authoritative_quality"
    )
    if len(authoritative) != 1 or authoritative[0].outcome != "pass":
        raise CheckpointConflictError(
            "Office validation evidence is not authoritative"
        )
    matches = [
        mutation
        for mutation in mutations
        if mutation.get("node_kind") == "file"
        and mutation.get("after_sha256") == report.candidate_sha256
        and isinstance(mutation.get("relative_path"), str)
        and str(mutation["relative_path"]).casefold().endswith(
            f".{report.document_format}"
        )
    ]
    if len(matches) != 1:
        raise CheckpointConflictError(
            "Office validation evidence does not identify one committed file"
        )
    return str(matches[0]["relative_path"]), report.to_dict()


def _verify_committed_mutation(
    workspace_root: str,
    mutation: dict[str, Any],
) -> None:
    """Prove metadata still describes the visible committed workspace state."""

    raw_relative = mutation.get("relative_path")
    if not isinstance(raw_relative, str) or "\\" in raw_relative:
        raise CheckpointConflictError("Workspace mutation path is not canonical")
    relative = PurePosixPath(raw_relative)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise CheckpointConflictError("Workspace mutation path escapes the workspace")
    target = Path(workspace_root).joinpath(*relative.parts)
    operation = mutation.get("operation")
    node_kind = mutation.get("node_kind")

    if operation == "deleted":
        try:
            target.lstat()
        except FileNotFoundError:
            return
        raise CheckpointConflictError(
            f"Deleted workspace path was recreated before ledger commit: {raw_relative}"
        )

    try:
        before = target.lstat()
    except FileNotFoundError as exc:
        raise CheckpointConflictError(
            f"Committed workspace path disappeared before ledger commit: {raw_relative}"
        ) from exc
    actual_mode = stat.S_IMODE(before.st_mode)
    if actual_mode != mutation.get("after_mode"):
        raise CheckpointConflictError(
            f"Committed workspace mode differs from ledger evidence: {raw_relative}"
        )

    if node_kind == "directory":
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise CheckpointConflictError(
                f"Committed workspace directory changed type: {raw_relative}"
            )
        return
    if node_kind == "symlink":
        if not stat.S_ISLNK(before.st_mode):
            raise CheckpointConflictError(
                f"Committed workspace symlink changed type: {raw_relative}"
            )
        link_target = os.readlink(target)
        expected_target = mutation.get("link_target")
        link_bytes = link_target.encode("utf-8")
        if (
            link_target != expected_target
            or len(link_bytes) != mutation.get("after_size")
            or hashlib.sha256(link_bytes).hexdigest()
            != mutation.get("after_sha256")
        ):
            raise CheckpointConflictError(
                f"Committed symbolic link differs from ledger evidence: {raw_relative}"
            )
        return
    if node_kind != "file" or not stat.S_ISREG(before.st_mode):
        raise CheckpointConflictError(
            f"Committed workspace file changed type: {raw_relative}"
        )

    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise CheckpointConflictError(
                f"Committed workspace file changed while opening: {raw_relative}"
            )
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(handle.fileno())
    visible_after = target.lstat()
    if (
        opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
        or not stat.S_ISREG(visible_after.st_mode)
        or visible_after.st_dev != after.st_dev
        or visible_after.st_ino != after.st_ino
        or size != mutation.get("after_size")
        or digest.hexdigest() != mutation.get("after_sha256")
    ):
        raise CheckpointConflictError(
            f"Committed workspace file differs from ledger evidence: {raw_relative}"
        )


async def record_tool_checkpoint_effects(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job: GenerationJob,
    binding: TurnCheckpointBinding | None,
    tool_id: str,
    call_id: str,
    metadata: dict[str, Any] | None,
) -> int:
    """Persist a successful tool's local mutations before TOOL_RESULT is visible."""

    if binding is None:
        return 0
    payload = metadata or {}
    journal_token = payload.pop("_checkpoint_journal", None)
    raw_office_report = payload.pop("_office_validation_report", None)
    mutations = payload.get("workspace_mutations")
    if mutations is None:
        mutations = []
    if not isinstance(mutations, list) or not all(
        isinstance(item, dict) for item in mutations
    ):
        raise CheckpointConflictError("Tool returned an invalid workspace mutation ledger")

    for mutation in mutations:
        _verify_committed_mutation(binding.workspace_root, mutation)
    office_report = _validated_office_report(
        raw_office_report,
        binding=binding,
        mutations=mutations,
    )

    recorded = 0
    direct_paths = [
        *_metadata_paths(payload.get("written_files"), field="written_files"),
        *_metadata_paths(payload.get("deleted_files"), field="deleted_files"),
    ]
    async with session_factory() as db:
        async with db.begin():
            existing = list(
                (
                    await db.execute(
                        select(CheckpointChange).where(
                            CheckpointChange.checkpoint_id == binding.checkpoint_id,
                            CheckpointChange.call_id == call_id,
                        )
                    )
                ).scalars()
            )
            by_path = {item.relative_path: item for item in existing}
            for mutation in mutations:
                relative_path = mutation.get("relative_path")
                if not isinstance(relative_path, str):
                    raise CheckpointConflictError(
                        "Workspace mutation is missing relative_path"
                    )
                replay = by_path.get(relative_path)
                if replay is not None:
                    if not _same_change(replay, mutation):
                        raise CheckpointConflictError(
                            "Tool call mutation replay has different evidence"
                        )
                    if (
                        office_report is not None
                        and office_report[0] == relative_path
                        and dict(replay.details or {}).get("office_validation")
                        != office_report[1]
                    ):
                        raise CheckpointConflictError(
                            "Office validation replay has different evidence"
                        )
                    continue
                details: dict[str, Any] = {"tool": tool_id}
                if mutation.get("node_kind") == "symlink":
                    details["link_target"] = mutation.get("link_target")
                if office_report is not None and office_report[0] == relative_path:
                    details["office_validation"] = office_report[1]
                await record_checkpoint_change(
                    db,
                    checkpoint_id=binding.checkpoint_id,
                    turn_run_id=binding.turn_run_id,
                    operation=mutation.get("operation"),
                    node_kind=mutation.get("node_kind"),
                    relative_path=relative_path,
                    before_version_id=mutation.get("before_version_id"),
                    before_sha256=mutation.get("before_sha256"),
                    before_mode=mutation.get("before_mode"),
                    after_sha256=mutation.get("after_sha256"),
                    after_mode=mutation.get("after_mode"),
                    after_size=mutation.get("after_size"),
                    call_id=call_id,
                    details=details,
                )
                recorded += 1

            if (
                payload.get("direct_workspace_execution")
                and (
                    direct_paths
                    or not bool(payload.get("artifact_tracking_complete", True))
                )
            ):
                await record_irreversible_side_effect(
                    db,
                    checkpoint_id=binding.checkpoint_id,
                    turn_run_id=binding.turn_run_id,
                    source=tool_id,
                    operation="direct_workspace_mutation",
                    audit_id=call_id,
                )

    if recorded:
        job.publish_lifecycle(
            "workspace.committed",
            {"tool": tool_id, "mutation_count": recorded},
            call_id=call_id,
            checkpoint_id=binding.checkpoint_id,
        )
    if journal_token is not None:
        from app.tool.workspace_transaction import (
            cleanup_committed_checkpoint_journal,
        )

        try:
            cleanup_committed_checkpoint_journal(
                str(journal_token),
                expected_checkpoint_id=binding.checkpoint_id,
            )
        except Exception:
            # The database ledger is already durable.  Retaining the journal is
            # safe and startup recovery will retry idempotently.
            logger.warning(
                "Could not remove committed checkpoint journal %s",
                journal_token,
                exc_info=True,
            )
    return recorded


async def finish_turn_checkpoint(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job: GenerationJob,
    binding: TurnCheckpointBinding | None,
    status: TurnFinishStatus,
    response_message_id: str | None,
    ledger_failed: bool = False,
) -> None:
    """Close the persistence boundary before DONE is published."""

    if binding is None:
        return
    final_state: str
    async with session_factory() as db:
        async with db.begin():
            checkpoint = await db.get(SessionCheckpoint, binding.checkpoint_id)
            if checkpoint is None:
                raise CheckpointConflictError("Turn checkpoint disappeared")
            if ledger_failed and checkpoint.state in {"prepared", "committing"}:
                checkpoint = await transition_checkpoint(
                    db,
                    checkpoint.id,
                    target_state="failed",
                )
            elif checkpoint.state == "committing":
                checkpoint = await transition_checkpoint(
                    db,
                    checkpoint.id,
                    target_state="finalized",
                )
            elif checkpoint.state not in {"finalized", "failed"}:
                raise CheckpointConflictError(
                    f"Cannot finish turn from checkpoint state {checkpoint.state}"
                )
            final_state = checkpoint.state
            await finish_turn(
                db,
                binding.turn_run_id,
                status=status,
                response_message_id=response_message_id,
            )
    job.publish_lifecycle(
        "checkpoint.finalized" if final_state == "finalized" else "checkpoint.failed",
        {"state": final_state, "turn_status": status},
        message_id=response_message_id,
        checkpoint_id=binding.checkpoint_id,
    )


async def recover_checkpoint_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    transaction_recovery: WorkspaceTransactionRecoveryReport | None = None,
) -> dict[str, int]:
    """Converge independent crash bridges without trusting a bad sibling."""

    from app.streaming.manager import GenerationJob
    from app.tool.workspace_transaction import (
        committed_checkpoint_journal_action,
        committed_checkpoint_journal_metadata,
        describe_workspace_recovery_blocker,
        scan_committed_checkpoint_journals_isolated,
    )
    from app.runtime.rewind import (
        recover_committed_rewind_journal,
        recover_stale_rewind_intents,
    )

    scan = await asyncio.to_thread(scan_committed_checkpoint_journals_isolated)
    blocked_by_token = {
        blocker.token: blocker
        for blocker in (() if transaction_recovery is None else transaction_recovery.blocked)
    }
    for blocker in scan.blocked:
        previous = blocked_by_token.get(blocker.token)
        if previous is None or (
            previous.provenance_unknown and not blocker.provenance_unknown
        ):
            blocked_by_token[blocker.token] = blocker

    recovered_journals = 0
    recovered_rewind_journals = 0
    committed_rewind_checkpoint_ids: set[str] = set()
    for token, payload in scan.journals:
        # Transaction recovery is the authority for proving that a committed
        # journal still belongs to the live workspace.  A journal can be
        # structurally readable here even though that earlier pass preserved
        # it after an identity or cleanup failure.  Replaying it would attach
        # untrusted filesystem effects to the database and could then delete
        # the only recovery evidence, so leave every pre-blocked token alone.
        if token in blocked_by_token:
            logger.error(
                "Checkpoint journal %s remains blocked by transaction recovery",
                token,
            )
            continue
        trusted_provenance = False
        try:
            action, rewind_checkpoint_ids = committed_checkpoint_journal_action(payload)
            if action == "rewind":
                # Capture ownership before replay. A failed replay remains an
                # exclusion for stale-intent compensation below.
                committed_rewind_checkpoint_ids.update(rewind_checkpoint_ids)
                await recover_committed_rewind_journal(
                    session_factory,
                    token,
                    payload,
                )
                blocked_by_token.pop(token, None)
                recovered_rewind_journals += 1
                continue

            runtime, metadata = committed_checkpoint_journal_metadata(payload)
            async with session_factory() as db:
                checkpoint = await db.get(SessionCheckpoint, runtime["checkpoint_id"])
                turn = await db.get(TurnRun, runtime["turn_run_id"])
                instance = await db.get(
                    WorkspaceInstance,
                    runtime["workspace_instance_id"],
                )
            if checkpoint is None or turn is None or instance is None:
                raise CheckpointConflictError(
                    "Committed filesystem journal has no matching database owner"
                )
            raw_workspace = payload.get("workspace")
            if (
                checkpoint.turn_run_id != turn.id
                or checkpoint.workspace_instance_id != instance.id
                or checkpoint.root_turn_id != runtime["root_turn_id"]
                or turn.session_id != runtime["session_id"]
                or instance.root_path != raw_workspace
            ):
                raise CheckpointConflictError(
                    "Committed filesystem journal provenance does not match the ledger"
                )
            trusted_provenance = True
            binding = TurnCheckpointBinding(
                root_turn_id=turn.root_turn_id,
                turn_run_id=turn.id,
                checkpoint_id=checkpoint.id,
                workspace_instance_id=instance.id,
                workspace_root=instance.root_path,
            )
            recovery_job = GenerationJob(
                turn.stream_id or f"recovery-{turn.id}",
                turn.session_id,
                invocation_source="unknown",
                root_turn_id=turn.root_turn_id,
                turn_run_id=turn.id,
                parent_turn_id=turn.parent_turn_id,
                workspace_instance_id=instance.id,
            )
            metadata["_checkpoint_journal"] = token
            await record_tool_checkpoint_effects(
                session_factory,
                job=recovery_job,
                binding=binding,
                tool_id=runtime["tool_operation"],
                call_id=runtime["call_id"],
                metadata=metadata,
            )
            await finish_turn_checkpoint(
                session_factory,
                job=recovery_job,
                binding=binding,
                status="failed",
                response_message_id=runtime["message_id"],
            )
            blocked_by_token.pop(token, None)
            recovered_journals += 1
        except Exception as exc:
            blocker = describe_workspace_recovery_blocker(
                token,
                payload,
                exc,
                trusted_provenance=trusted_provenance,
            )
            blocked_by_token[token] = blocker
            logger.error(
                "Checkpoint journal %s was preserved during startup: %s",
                token,
                exc,
            )

    blockers = tuple(blocked_by_token.values())
    blocked_checkpoint_ids = {
        checkpoint_id
        for blocker in blockers
        for checkpoint_id in blocker.checkpoint_ids
    }
    blocked_turn_ids = {
        turn_id for blocker in blockers for turn_id in blocker.turn_run_ids
    }
    blocked_owner_workspace_ids = {
        workspace_id
        for blocker in blockers
        for workspace_id in blocker.workspace_instance_ids
    }
    unknown_journal_provenance = any(
        blocker.provenance_unknown for blocker in blockers
    )
    committed_rewind_checkpoint_ids.update(blocked_checkpoint_ids)

    if unknown_journal_provenance:
        compensated_rewind_intents = 0
        logger.error(
            "Deferred stale rewind compensation because a preserved journal "
            "has unknown provenance"
        )
    else:
        compensated_rewind_intents = await recover_stale_rewind_intents(
            session_factory,
            committed_rewind_checkpoint_ids,
        )

    finalized_stale = 0
    failed_stale = 0
    if unknown_journal_provenance:
        logger.error(
            "Deferred stale turn cleanup because a preserved journal has "
            "unknown provenance"
        )
    else:
        async with session_factory() as db, db.begin():
            stale_turns = list(
                (
                    await db.execute(
                        select(TurnRun).where(TurnRun.status == "running")
                    )
                ).scalars()
            )
            for turn in stale_turns:
                if (
                    turn.id in blocked_turn_ids
                    or turn.workspace_instance_id in blocked_owner_workspace_ids
                ):
                    continue
                checkpoint = (
                    await db.execute(
                        select(SessionCheckpoint).where(
                            SessionCheckpoint.turn_run_id == turn.id
                        )
                    )
                ).scalar_one_or_none()
                if checkpoint is not None and (
                    checkpoint.id in blocked_checkpoint_ids
                    or checkpoint.workspace_instance_id
                    in blocked_owner_workspace_ids
                ):
                    continue
                if checkpoint is None:
                    await finish_turn(db, turn.id, status="failed")
                    failed_stale += 1
                    continue
                if checkpoint.state not in {"prepared", "committing"}:
                    if checkpoint.state == "finalized":
                        await finish_turn(db, turn.id, status="failed")
                    continue
                change_count = int(
                    (
                        await db.execute(
                            select(func.count(CheckpointChange.id)).where(
                                CheckpointChange.checkpoint_id == checkpoint.id
                            )
                        )
                    ).scalar_one()
                )
                if change_count or checkpoint.has_irreversible_side_effects:
                    await transition_checkpoint(
                        db,
                        checkpoint.id,
                        target_state="finalized",
                    )
                    finalized_stale += 1
                else:
                    await transition_checkpoint(
                        db,
                        checkpoint.id,
                        target_state="failed",
                    )
                    failed_stale += 1
                await finish_turn(db, turn.id, status="failed")

    # Failed empty checkpoints own no useful rewind state. Release their empty
    # pin owners before reconciling every remaining database owner.
    blocked_workspace_ids: set[str] = set(blocked_owner_workspace_ids)
    reconciled_pins = 0
    if unknown_journal_provenance:
        logger.error(
            "Deferred checkpoint pin cleanup because a preserved journal has "
            "unknown provenance"
        )
    else:
        async with session_factory() as db, db.begin():
            failed_checkpoints = list(
                (
                    await db.execute(
                        select(SessionCheckpoint).where(
                            SessionCheckpoint.state == "failed",
                            SessionCheckpoint.pin_state == "pinned",
                        )
                    )
                ).scalars()
            )
            for checkpoint in failed_checkpoints:
                if (
                    checkpoint.id in blocked_checkpoint_ids
                    or checkpoint.workspace_instance_id
                    in blocked_owner_workspace_ids
                ):
                    continue
                retained_change_count = int(
                    (
                        await db.execute(
                            select(func.count(CheckpointChange.id)).where(
                                CheckpointChange.checkpoint_id == checkpoint.id
                            )
                        )
                    ).scalar_one()
                )
                if not retained_change_count and not checkpoint.has_irreversible_side_effects:
                    try:
                        await release_checkpoint_pin(db, checkpoint.id)
                    except (CheckpointLedgerError, FileVersionError, OSError) as exc:
                        blocked_workspace_ids.add(checkpoint.workspace_instance_id)
                        logger.error(
                            "Checkpoint pin release deferred for unavailable workspace %s: %s",
                            checkpoint.workspace_instance_id,
                            exc,
                        )

            workspace_ids = list(
                (
                    await db.execute(
                        select(SessionCheckpoint.workspace_instance_id).distinct()
                    )
                ).scalars()
            )
            for workspace_id in workspace_ids:
                if workspace_id in blocked_owner_workspace_ids:
                    continue
                try:
                    reconciled_pins += await reconcile_workspace_checkpoint_pins(
                        db,
                        workspace_id,
                    )
                except (CheckpointLedgerError, FileVersionError, OSError) as exc:
                    blocked_workspace_ids.add(workspace_id)
                    logger.error(
                        "Checkpoint reconciliation deferred for unavailable workspace %s: %s",
                        workspace_id,
                        exc,
                    )

    return {
        "journals": recovered_journals,
        "rewind_journals": recovered_rewind_journals,
        "rewind_intents_compensated": compensated_rewind_intents,
        "stale_finalized": finalized_stale,
        "stale_failed": failed_stale,
        "pins_reconciled": reconciled_pins,
        "workspaces_blocked": len(blocked_workspace_ids)
        + int(unknown_journal_provenance),
        "journals_blocked": len(blockers),
    }


__all__ = [
    "TurnCheckpointBinding",
    "admit_turn_checkpoint",
    "checkpoint_runtime_enabled",
    "finish_turn_checkpoint",
    "record_tool_checkpoint_effects",
    "recover_checkpoint_runtime",
]
