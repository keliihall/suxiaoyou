from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import release_features
from app.models.idempotency_record import IdempotencyRecord
from app.models.message import Message, Part
from app.models.goal_run import GoalRun
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.session_goal import SessionGoal
from app.models.todo import Todo
from app.models.turn_run import TurnRun
from app.runtime import rewind as rewind_module
from app.runtime.checkpoint_runtime import (
    TurnCheckpointBinding,
    admit_turn_checkpoint,
    finish_turn_checkpoint,
    record_tool_checkpoint_effects,
)
from app.runtime.rewind import (
    RewindBusyError,
    RewindConflictError,
    RewindDisabledError,
    RewindProvenanceError,
    RewindService,
    recover_committed_rewind_journal,
    recover_stale_rewind_intents,
)
from app.schemas.agent import AgentInfo
from app.storage.checkpoints import (
    create_root_turn,
    record_irreversible_side_effect,
    transition_checkpoint,
)
from app.streaming.manager import GenerationJob, StreamManager
from app.tool.context import ToolContext
from app.tool.workspace_transaction import (
    WorkspaceMutationTransaction,
    committed_checkpoint_journal_action,
    list_committed_checkpoint_journals,
)
from app.utils.id import generate_ulid


Mutator = Callable[[Path], None]


async def _add_message(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
    *,
    role: str,
) -> str:
    message_id = generate_ulid()
    async with session_factory() as db:
        async with db.begin():
            db.add(Message(id=message_id, session_id=session_id, data={"role": role}))
    return message_id


async def _setup_session(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Path,
    *,
    session_id: str = "session",
) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id=session_id,
                    directory=str(workspace),
                    title="rewind",
                    version="1.1.0",
                )
            )


async def _commit_checkpoint(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Path,
    *,
    session_id: str,
    stream_id: str,
    todo_snapshot: list[dict[str, object]],
    mutate: Mutator,
    goal_id: str | None = None,
    goal_run_id: str | None = None,
) -> TurnCheckpointBinding:
    anchor = await _add_message(session_factory, session_id, role="user")
    job = GenerationJob(
        stream_id,
        session_id,
        invocation_source="desktop",
        invocation_source_id="desktop",
        goal_id=goal_id,
        goal_run_id=goal_run_id,
        goal_session_id=session_id if goal_id is not None else None,
    )
    binding = await admit_turn_checkpoint(
        session_factory,
        job=job,
        workspace=str(workspace),
        request_message_id=anchor,
        todo_snapshot=todo_snapshot,
    )
    assert binding is not None
    context = ToolContext(
        session_id=session_id,
        message_id=anchor,
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id=f"call-{stream_id}",
        workspace=str(workspace),
        root_turn_id=binding.root_turn_id,
        turn_run_id=binding.turn_run_id,
        checkpoint_id=binding.checkpoint_id,
        workspace_instance_id=binding.workspace_instance_id,
    )
    transaction = WorkspaceMutationTransaction(workspace, context, operation="test")
    staged = transaction.prepare()
    mutate(staged)
    commit = transaction.commit()
    await record_tool_checkpoint_effects(
        session_factory,
        job=job,
        binding=binding,
        tool_id="test",
        call_id=context.call_id,
        metadata=commit.metadata,
    )
    assistant = await _add_message(session_factory, session_id, role="assistant")
    await finish_turn_checkpoint(
        session_factory,
        job=job,
        binding=binding,
        status="completed",
        response_message_id=assistant,
    )
    return binding


@pytest.fixture
def released_rewind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(release_features, "V11_CHECKPOINTS_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", True)
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))


@pytest.mark.asyncio
async def test_rewind_invalidates_completed_acp_replay_bound_to_removed_message(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace)
    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream",
        todo_snapshot=[],
        mutate=lambda stage: (stage / "file.txt").write_text(
            "after",
            encoding="utf-8",
        ),
    )
    bound_message_id = "acp-bound-after-checkpoint"
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Message(
                    id=bound_message_id,
                    session_id="session",
                    data={"role": "user", "acp_message_id": "acp-rewind-key"},
                )
            )
            db.add(
                Part(
                    id="acp-bound-after-checkpoint-part",
                    message_id=bound_message_id,
                    session_id="session",
                    data={"type": "text", "text": "must not replay after rewind"},
                )
            )
            db.add(
                IdempotencyRecord(
                    scope="acp.prompt:session",
                    request_key="acp-rewind-key",
                    request_hash="hash-acp-rewind-key",
                    status="completed",
                    response={
                        "userMessageId": bound_message_id,
                        "staleReplayPayload": "must be cleared",
                    },
                )
            )

    service = RewindService(session_factory, stream_manager=StreamManager())
    await service.execute(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )

    assert target.read_text(encoding="utf-8") == "before"
    async with session_factory() as db:
        record = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope == "acp.prompt:session",
                    IdempotencyRecord.request_key == "acp-rewind-key",
                )
            )
        ).scalar_one()
        assert await db.get(Message, bound_message_id) is None
    assert record.status == "interrupted"
    assert record.response == {}
    assert record.error_message == "acp_prompt_history_changed"


@pytest.mark.asyncio
async def test_rewind_restores_multifile_directories_conversation_todos_and_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "modified.txt").write_text("original", encoding="utf-8")
    (workspace / "deleted.txt").write_text("bring me back", encoding="utf-8")
    (workspace / "gone").mkdir()
    await _setup_session(session_factory, workspace)
    before_message = await _add_message(session_factory, "session", role="user")
    old_todo = {
        "id": "todo-old",
        "goal_id": None,
        "content": "original task",
        "status": "pending",
        "active_form": "working",
        "position": 0,
    }
    async with session_factory() as db:
        async with db.begin():
            db.add(Todo(session_id="session", **old_todo))

    def first(stage: Path) -> None:
        (stage / "modified.txt").write_text("first", encoding="utf-8")
        (stage / "created.txt").write_text("created", encoding="utf-8")
        (stage / "deleted.txt").unlink()
        (stage / "scratch").mkdir()
        (stage / "gone").rmdir()

    first_binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream-1",
        todo_snapshot=[old_todo],
        mutate=first,
    )
    async with session_factory() as db:
        async with db.begin():
            await db.execute(Todo.__table__.delete().where(Todo.session_id == "session"))
            db.add(
                Todo(
                    id="todo-new",
                    session_id="session",
                    content="later task",
                    status="in_progress",
                    active_form="later",
                    position=0,
                )
            )

    def second(stage: Path) -> None:
        (stage / "modified.txt").write_text("second", encoding="utf-8")
        (stage / "created.txt").write_text("created twice", encoding="utf-8")
        (stage / "extra.txt").write_text("extra", encoding="utf-8")

    second_binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream-2",
        todo_snapshot=[],
        mutate=second,
    )

    manager = StreamManager()
    service = RewindService(session_factory, stream_manager=manager)
    listed = await service.list(
        session_id="session",
        workspace_instance_id=first_binding.workspace_instance_id,
    )
    assert [item.checkpoint_id for item in listed] == [
        second_binding.checkpoint_id,
        first_binding.checkpoint_id,
    ]
    preview = await service.preview(
        session_id="session",
        workspace_instance_id=first_binding.workspace_instance_id,
        checkpoint_id=first_binding.checkpoint_id,
    )
    assert preview.can_execute
    assert not preview.conflicts
    assert set(preview.affected_checkpoint_ids) == {
        first_binding.checkpoint_id,
        second_binding.checkpoint_id,
    }
    assert {path.action for path in preview.paths} >= {
        "restore_file",
        "remove",
        "create_directory",
    }

    result = await service.execute(
        session_id="session",
        workspace_instance_id=first_binding.workspace_instance_id,
        checkpoint_id=first_binding.checkpoint_id,
    )
    assert not result.already_rewound
    assert (workspace / "modified.txt").read_text(encoding="utf-8") == "original"
    assert (workspace / "deleted.txt").read_text(encoding="utf-8") == "bring me back"
    assert not (workspace / "created.txt").exists()
    assert not (workspace / "extra.txt").exists()
    assert not (workspace / "scratch").exists()
    assert (workspace / "gone").is_dir()
    assert list_committed_checkpoint_journals() == []

    async with session_factory() as db:
        checkpoints = list(
            (
                await db.execute(
                    select(SessionCheckpoint).order_by(SessionCheckpoint.sequence)
                )
            ).scalars()
        )
        turns = list((await db.execute(select(TurnRun))).scalars())
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.session_id == "session")
                    .order_by(Message.time_created, Message.id)
                )
            ).scalars()
        )
        todos = list((await db.execute(select(Todo))).scalars())
    assert all(item.state == "rewound" and item.pin_state == "released" for item in checkpoints)
    assert all(item.status == "rewound" for item in turns)
    assert [message.id for message in messages] == [before_message]
    assert [(todo.id, todo.content, todo.status) for todo in todos] == [
        ("todo-old", "original task", "pending")
    ]
    target_checkpoint = next(
        item for item in checkpoints if item.id == first_binding.checkpoint_id
    )
    restored = {
        item["relative_path"]: item
        for item in target_checkpoint.details["rewind_result"]["restored_paths"]
    }
    assert restored["modified.txt"]["sha256"] == hashlib.sha256(
        b"original"
    ).hexdigest()
    assert restored["modified.txt"]["node_kind"] == "file"
    assert restored["deleted.txt"]["sha256"] == hashlib.sha256(
        b"bring me back"
    ).hexdigest()
    assert restored["created.txt"]["exists"] is False
    assert restored["gone"]["node_kind"] == "directory"

    replay = await service.execute(
        session_id="session",
        workspace_instance_id=first_binding.workspace_instance_id,
        checkpoint_id=first_binding.checkpoint_id,
    )
    assert replay.already_rewound
    assert replay.changed_paths == result.changed_paths
    assert (workspace / "modified.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_rewind_conflict_changes_no_files_or_checkpoint_state(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.txt"
    target.write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace)

    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream",
        todo_snapshot=[],
        mutate=lambda stage: (stage / "report.txt").write_text("after", encoding="utf-8"),
    )
    target.write_text("outside edit", encoding="utf-8")
    service = RewindService(session_factory, stream_manager=StreamManager())
    preview = await service.preview(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )
    assert not preview.can_execute
    assert [item.relative_path for item in preview.conflicts] == ["report.txt"]
    with pytest.raises(RewindConflictError):
        await service.execute(
            session_id="session",
            workspace_instance_id=binding.workspace_instance_id,
            checkpoint_id=binding.checkpoint_id,
        )
    assert target.read_text(encoding="utf-8") == "outside edit"
    async with session_factory() as db:
        checkpoint = await db.get(SessionCheckpoint, binding.checkpoint_id)
    assert checkpoint is not None and checkpoint.state == "finalized"


@pytest.mark.asyncio
async def test_rewind_removes_checkpoint_created_symlink_and_only_discloses_external_effect(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text("target", encoding="utf-8")
    await _setup_session(session_factory, workspace)

    def create_link(stage: Path) -> None:
        (stage / "report-link").symlink_to("target.txt")

    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="symlink",
        todo_snapshot=[],
        mutate=create_link,
    )
    async with session_factory() as db:
        async with db.begin():
            await record_irreversible_side_effect(
                db,
                checkpoint_id=binding.checkpoint_id,
                turn_run_id=binding.turn_run_id,
                source="email",
                operation="send",
                audit_id="audit-1",
            )

    service = RewindService(session_factory, stream_manager=StreamManager())
    preview = await service.preview(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )
    assert preview.can_execute
    assert preview.paths == (
        rewind_module.RewindPath(
            relative_path="report-link",
            action="remove",
            current_kind="symlink",
            desired_kind=None,
        ),
    )
    assert preview.external_side_effects == (
        {
            "checkpoint_id": binding.checkpoint_id,
            "source": "email",
            "operation": "send",
            "audit_id": "audit-1",
        },
    )
    result = await service.execute(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )
    assert not (workspace / "report-link").exists()
    assert result.external_side_effects == preview.external_side_effects
    assert list_committed_checkpoint_journals() == []


@pytest.mark.asyncio
async def test_rewind_gate_and_all_quiescence_guards_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace)
    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="checkpoint",
        todo_snapshot=[],
        mutate=lambda stage: (stage / "file.txt").write_text("after", encoding="utf-8"),
    )

    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", False)
    closed = RewindService(session_factory, stream_manager=StreamManager())
    with pytest.raises(RewindDisabledError):
        await closed.list(
            session_id="session",
            workspace_instance_id=binding.workspace_instance_id,
        )
    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_CHECKPOINTS_RELEASED", False)
    with pytest.raises(RewindDisabledError):
        await closed.list(
            session_id="session",
            workspace_instance_id=binding.workspace_instance_id,
        )
    monkeypatch.setattr(release_features, "V11_CHECKPOINTS_RELEASED", True)

    active_manager = StreamManager()
    active_manager.create_job("active", "session")
    active_service = RewindService(session_factory, stream_manager=active_manager)
    preview = await active_service.preview(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )
    assert "session has an active generation job" in preview.blockers
    with pytest.raises(RewindBusyError):
        await active_service.execute(
            session_id="session",
            workspace_instance_id=binding.workspace_instance_id,
            checkpoint_id=binding.checkpoint_id,
        )

    manager = StreamManager()
    async with session_factory() as db:
        async with db.begin():
            running = await create_root_turn(
                db,
                session_id="session",
                workspace_instance_id=binding.workspace_instance_id,
                turn_id="running-turn",
            )
            db.add(
                SessionGoal(
                    id="goal",
                    session_id="session",
                    objective="finish",
                    status="active",
                    run_state="idle",
                )
            )
    service = RewindService(session_factory, stream_manager=manager)
    preview = await service.preview(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )
    assert "database has a running turn for the session/workspace" in preview.blockers
    assert "session Goal must be idle and non-active" in preview.blockers
    async with session_factory() as db:
        async with db.begin():
            running = await db.get(TurnRun, "running-turn")
            goal = await db.get(SessionGoal, "goal")
            assert running is not None and goal is not None
            running.status = "cancelled"
            goal.status = "paused"
    ready = await service.preview(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )
    assert ready.can_execute


@pytest.mark.asyncio
async def test_rewind_rechecks_final_state_after_transaction_preparation(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace)
    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream",
        todo_snapshot=[],
        mutate=lambda stage: (stage / "file.txt").write_text("after", encoding="utf-8"),
    )

    original_prepare_paths = WorkspaceMutationTransaction.prepare_paths

    def edit_immediately_before_prepare(
        transaction: WorkspaceMutationTransaction,
        *args: object,
        **kwargs: object,
    ) -> Path:
        # This lands after execute's first preflight but is deliberately
        # accepted into the transaction baseline.  Only the required second
        # final-state check can distinguish it from the checkpoint evidence.
        target.write_text("raced before prepare", encoding="utf-8")
        return original_prepare_paths(transaction, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        WorkspaceMutationTransaction,
        "prepare_paths",
        edit_immediately_before_prepare,
    )
    service = RewindService(session_factory, stream_manager=StreamManager())
    with pytest.raises(RewindConflictError, match="staging was prepared"):
        await service.execute(
            session_id="session",
            workspace_instance_id=binding.workspace_instance_id,
            checkpoint_id=binding.checkpoint_id,
        )
    assert target.read_text(encoding="utf-8") == "raced before prepare"
    async with session_factory() as db:
        checkpoint = await db.get(SessionCheckpoint, binding.checkpoint_id)
    assert checkpoint is not None and checkpoint.state == "finalized"
    assert list_committed_checkpoint_journals() == []


@pytest.mark.asyncio
async def test_rewind_restores_goal_pointer_and_paused_state_without_erasing_usage(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace)
    completed_at = datetime.now(timezone.utc)
    async with session_factory() as db:
        async with db.begin():
            db.add(
                SessionGoal(
                    id="goal",
                    session_id="session",
                    objective="deliver",
                    status="complete",
                    run_state="idle",
                    revision=7,
                    tokens_used=321,
                    cost_used_microusd=654,
                    time_used_seconds=87,
                    continuation_count=4,
                    no_progress_count=3,
                    blocker_streak=2,
                    consecutive_error_count=5,
                    blocker_code="old-blocker",
                    blocker_message="old blocker message",
                    needs_review=True,
                    next_retry_at=completed_at,
                    completion_summary="was complete",
                    completion_evidence=[{"kind": "test"}],
                    last_run_id="goal-run",
                    last_stream_id="goal-stream",
                    time_completed=completed_at,
                )
            )
            db.add(
                GoalRun(
                    id="goal-run",
                    goal_id="goal",
                    ordinal=1,
                    goal_revision=7,
                    idempotency_key="goal-run-key",
                    stream_id="goal-stream",
                    trigger="initial",
                    status="completed",
                    tokens_used=111,
                    cost_used_microusd=222,
                    active_seconds=33,
                    time_started=completed_at,
                    time_finished=completed_at,
                )
            )
    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream",
        todo_snapshot=[],
        goal_id="goal",
        goal_run_id="goal-run",
        mutate=lambda stage: (stage / "file.txt").write_text("after", encoding="utf-8"),
    )
    service = RewindService(session_factory, stream_manager=StreamManager())
    await service.execute(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
        checkpoint_id=binding.checkpoint_id,
    )
    async with session_factory() as db:
        goal = await db.get(SessionGoal, "goal")
        run = await db.get(GoalRun, "goal-run")
    assert goal is not None and run is not None
    assert (goal.status, goal.run_state, goal.revision) == ("paused", "idle", 8)
    assert (goal.last_run_id, goal.last_stream_id) == ("goal-run", "goal-stream")
    assert (
        goal.blocker_code,
        goal.blocker_message,
        goal.blocker_streak,
        goal.needs_review,
        goal.next_retry_at,
        goal.no_progress_count,
        goal.consecutive_error_count,
        goal.completion_summary,
        goal.completion_evidence,
        goal.time_completed,
    ) == (None, None, 0, False, None, 0, 0, None, None, None)
    assert (
        goal.tokens_used,
        goal.cost_used_microusd,
        goal.time_used_seconds,
        goal.continuation_count,
    ) == (321, 654, 87, 4)
    assert (run.tokens_used, run.cost_used_microusd, run.active_seconds) == (111, 222, 33)


@pytest.mark.asyncio
async def test_rewind_rejects_cross_session_checkpoint_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace, session_id="owner")
    await _setup_session(session_factory, workspace, session_id="foreign")
    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="owner",
        stream_id="stream",
        todo_snapshot=[],
        mutate=lambda stage: (stage / "file.txt").write_text("after", encoding="utf-8"),
    )
    service = RewindService(session_factory, stream_manager=StreamManager())
    with pytest.raises(RewindProvenanceError):
        await service.preview(
            session_id="foreign",
            workspace_instance_id=binding.workspace_instance_id,
            checkpoint_id=binding.checkpoint_id,
        )


@pytest.mark.asyncio
async def test_committed_rewind_journal_recovery_finishes_database_idempotently(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace)
    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream",
        todo_snapshot=[],
        mutate=lambda stage: (stage / "file.txt").write_text("after", encoding="utf-8"),
    )
    service = RewindService(session_factory, stream_manager=StreamManager())
    original_complete = rewind_module._complete_database

    async def crash_after_files(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated hard-exit boundary")

    monkeypatch.setattr(rewind_module, "_complete_database", crash_after_files)
    with pytest.raises(RuntimeError, match="simulated"):
        await service.execute(
            session_id="session",
            workspace_instance_id=binding.workspace_instance_id,
            checkpoint_id=binding.checkpoint_id,
        )
    assert target.read_text(encoding="utf-8") == "before"
    async with session_factory() as db:
        checkpoint = await db.get(SessionCheckpoint, binding.checkpoint_id)
    assert checkpoint is not None and checkpoint.state == "rewinding"
    journals = list_committed_checkpoint_journals()
    assert len(journals) == 1
    token, payload = journals[0]
    assert committed_checkpoint_journal_action(payload) == (
        "rewind",
        (binding.checkpoint_id,),
    )

    monkeypatch.setattr(rewind_module, "_complete_database", original_complete)
    assert await recover_committed_rewind_journal(session_factory, token, payload)
    assert list_committed_checkpoint_journals() == []
    async with session_factory() as db:
        checkpoint = await db.get(SessionCheckpoint, binding.checkpoint_id)
    assert checkpoint is not None and checkpoint.state == "rewound"


@pytest.mark.asyncio
async def test_stale_precommit_rewind_intent_is_compensated(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    released_rewind: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("before", encoding="utf-8")
    await _setup_session(session_factory, workspace)
    binding = await _commit_checkpoint(
        session_factory,
        workspace,
        session_id="session",
        stream_id="stream",
        todo_snapshot=[],
        mutate=lambda stage: (stage / "file.txt").write_text("after", encoding="utf-8"),
    )
    async with session_factory() as db:
        async with db.begin():
            await transition_checkpoint(
                db, binding.checkpoint_id, target_state="rewinding"
            )
    assert await recover_stale_rewind_intents(session_factory, []) == 1
    async with session_factory() as db:
        checkpoint = await db.get(SessionCheckpoint, binding.checkpoint_id)
    assert checkpoint is not None and checkpoint.state == "finalized"
