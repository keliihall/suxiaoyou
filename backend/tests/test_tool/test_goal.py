from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.goal_run import GoalRun
from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.models.todo import Todo
from app.session.input_queue import enqueue_session_input
from app.tool.builtin.goal import GetGoalTool, UpdateGoalTool
from app.tool.context import ToolContext
from app.streaming.manager import GenerationJob


@pytest.fixture
async def goal_tool_state(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'goal-tool.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=str(tmp_path), title="Goal tool"))
            db.add(
                SessionGoal(
                    id="goal",
                    session_id="session",
                    objective="Create a verified deliverable",
                    definition_of_done="The deliverable exists",
                    status="active",
                    run_state="running",
                    revision=2,
                    last_run_id="run",
                )
            )
            db.add(
                GoalRun(
                    id="run",
                    goal_id="goal",
                    ordinal=1,
                    goal_revision=2,
                    idempotency_key="goal-tool-run",
                    trigger="initial",
                    status="running",
                )
            )
            db.add(
                Todo(
                    id="todo",
                    session_id="session",
                    goal_id="goal",
                    content="Create deliverable",
                    status="in_progress",
                )
            )
    ctx = ToolContext(
        session_id="session",
        message_id="message",
        agent=object(),  # type: ignore[arg-type]
        call_id="update-goal-call",
        workspace=str(tmp_path),
        invocation_source="goal",
        invocation_source_id="goal",
        goal_id="goal",
        goal_run_id="run",
    )
    ctx._app_state = {"session_factory": factory}  # type: ignore[attr-defined]
    ctx._job = GenerationJob(  # type: ignore[attr-defined]
        "goal-tool-stream",
        "session",
        invocation_source="goal",
        goal_id="goal",
        goal_run_id="run",
    )
    try:
        yield factory, ctx, tmp_path
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_goal_returns_current_revision(goal_tool_state) -> None:
    _factory, ctx, _tmp_path = goal_tool_state
    result = await GetGoalTool().execute({}, ctx)
    assert result.success
    assert result.metadata["goal"]["id"] == "goal"
    assert result.metadata["goal"]["revision"] == 2
    assert "Create a verified deliverable" in result.output


@pytest.mark.asyncio
async def test_complete_requires_finished_goal_todos_and_real_file_evidence(
    goal_tool_state,
) -> None:
    factory, ctx, tmp_path = goal_tool_state
    deliverable = tmp_path / "deliverable.txt"
    deliverable.write_text("verified", encoding="utf-8")
    args = {
        "status": "complete",
        "expected_revision": 2,
        "summary": "Created and verified the deliverable",
        "evidence": [
            {
                "criterion": "The deliverable exists",
                "evidence": "Checked the generated file",
                "path": str(deliverable),
            }
        ],
    }

    rejected = await UpdateGoalTool().execute(args, ctx)
    assert rejected.success is False
    assert "Todo" in (rejected.error or "")

    async with factory() as db:
        async with db.begin():
            todo = await db.get(Todo, "todo")
            assert todo is not None
            todo.status = "completed"

    accepted = await UpdateGoalTool().execute(args, ctx)
    assert accepted.success
    assert accepted.metadata["goal"]["status"] == "complete"
    async with factory() as db:
        goal = await db.get(SessionGoal, "goal")
    assert goal is not None
    assert goal.status == "complete"
    assert goal.revision == 3
    assert goal.completion_evidence == args["evidence"]
    stored_evidence = goal.completion_evidence[0]  # type: ignore[index]
    assert stored_evidence["verification"] == "server-file-sha256"
    assert stored_evidence["size_bytes"] == len(b"verified")
    assert len(stored_evidence["sha256"]) == 64


@pytest.mark.asyncio
async def test_complete_rejects_model_only_text_evidence(goal_tool_state) -> None:
    factory, ctx, _tmp_path = goal_tool_state
    async with factory() as db:
        async with db.begin():
            todo = await db.get(Todo, "todo")
            assert todo is not None
            todo.status = "completed"

    result = await UpdateGoalTool().execute(
        {
            "status": "complete",
            "expected_revision": 2,
            "summary": "Claimed completion without a verifiable action",
            "evidence": [
                {
                    "criterion": "The deliverable exists",
                    "evidence": "I checked it myself",
                }
            ],
        },
        ctx,
    )

    assert result.success is False
    assert "file path or a successful tool call_id" in (result.error or "")


@pytest.mark.asyncio
async def test_complete_accepts_server_bound_successful_tool_evidence(
    goal_tool_state,
) -> None:
    factory, ctx, _tmp_path = goal_tool_state
    async with factory() as db:
        async with db.begin():
            todo = await db.get(Todo, "todo")
            assert todo is not None
            todo.status = "completed"
            message = Message(
                id="evidence-message",
                session_id="session",
                data={"role": "assistant"},
            )
            db.add(message)
            await db.flush()
            db.add(
                Part(
                    id="evidence-part",
                    message_id=message.id,
                    session_id="session",
                    data={
                        "type": "tool",
                        "tool": "read",
                        "call_id": "verified-read-call",
                        "state": {"status": "completed", "output": "verified"},
                    },
                )
            )

    evidence = [
        {
            "criterion": "The deliverable exists",
            "evidence": "Verified through a successful read",
            "call_id": "verified-read-call",
        }
    ]
    result = await UpdateGoalTool().execute(
        {
            "status": "complete",
            "expected_revision": 2,
            "summary": "Verified the deliverable",
            "evidence": evidence,
        },
        ctx,
    )

    assert result.success
    assert evidence[0]["verification"] == "server-successful-tool-call"
    assert evidence[0]["tool"] == "read"


@pytest.mark.asyncio
async def test_update_goal_rejects_non_goal_invocation(goal_tool_state) -> None:
    _factory, ctx, _tmp_path = goal_tool_state
    ctx.invocation_source = "desktop"
    result = await UpdateGoalTool().execute(
        {
            "status": "blocked",
            "expected_revision": 2,
            "summary": "Cannot continue",
            "evidence": [{"criterion": "Access", "evidence": "Missing access"}],
            "blocker_code": "missing_access",
            "blocker_message": "Credentials are required",
        },
        ctx,
    )
    assert result.success is False
    assert "active Goal runner" in (result.error or "")


@pytest.mark.asyncio
async def test_complete_rejects_file_evidence_outside_workspace(
    goal_tool_state,
) -> None:
    factory, ctx, tmp_path = goal_tool_state
    outside = tmp_path.parent / "outside-goal-evidence.txt"
    outside.write_text("not in the Goal workspace", encoding="utf-8")
    async with factory() as db:
        async with db.begin():
            todo = await db.get(Todo, "todo")
            assert todo is not None
            todo.status = "completed"

    result = await UpdateGoalTool().execute(
        {
            "status": "complete",
            "expected_revision": 2,
            "summary": "Claimed an out-of-scope artifact",
            "evidence": [
                {
                    "criterion": "Deliverable exists",
                    "evidence": "Checked a file outside the workspace",
                    "path": str(outside),
                }
            ],
        },
        ctx,
    )

    assert result.success is False
    assert "outside the active workspace" in (result.error or "")


@pytest.mark.asyncio
async def test_terminal_goal_update_rejects_already_queued_user_input(
    goal_tool_state,
) -> None:
    factory, ctx, _tmp_path = goal_tool_state
    async with factory() as db:
        async with db.begin():
            await enqueue_session_input(
                db,
                session_id="session",
                client_request_id="queued-before-completion",
                mode="queue",
                text="Please revise the output first",
                attachments=[],
                model_id=None,
                provider_id=None,
                agent="build",
                language="zh",
                workspace=None,
                reasoning=None,
                permission_presets=None,
                permission_rules=None,
                target_stream_id=None,
            )

    result = await UpdateGoalTool().execute(
        {
            "status": "blocked",
            "expected_revision": 2,
            "summary": "Tried to stop before applying user input",
            "evidence": [{"criterion": "Input", "evidence": "Not yet applied"}],
            "blocker_code": "premature_stop",
            "blocker_message": "Should not commit",
        },
        ctx,
    )

    assert result.success is False
    assert "real user input is queued" in (result.error or "")
    assert ctx._job.accepting_session_inputs is True  # type: ignore[attr-defined]
    assert ctx._job.execution_admission_open is True  # type: ignore[attr-defined]
    async with factory() as db:
        goal = await db.get(SessionGoal, "goal")
    assert goal is not None and goal.status == "active"
