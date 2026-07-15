from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.session import Session
from app.models.goal_run import GoalRun
from app.models.session_goal import SessionGoal
from app.session.goal_guard import (
    GoalBudgetGate,
    GoalExecutionGate,
    _remaining,
    block_goal_for_user_action,
    read_goal_budget_gate,
    read_goal_execution_gate,
    set_goal_waiting_user,
)
from app.session.tool_executor import ToolCallInfo, _execute_single
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext


@pytest.mark.asyncio
async def test_tool_context_execution_guard_defaults_open_for_ordinary_turns() -> None:
    ctx = ToolContext(
        session_id="session",
        message_id="message",
        agent=object(),  # type: ignore[arg-type]
        call_id="call",
    )
    assert await ctx.execution_allowed() == (True, None)


@pytest.mark.asyncio
async def test_tool_context_execution_guard_propagates_safe_stop_reason() -> None:
    ctx = ToolContext(
        session_id="session",
        message_id="message",
        agent=object(),  # type: ignore[arg-type]
        call_id="call",
        _execution_guard_fn=lambda: _deny(),
    )
    assert await ctx.execution_allowed() == (False, "Goal is paused")


async def _deny() -> tuple[bool, str | None]:
    return False, "Goal is paused"


@pytest.fixture
async def goal_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Goal guard"))
            db.add(
                SessionGoal(
                    id="goal",
                    session_id="session",
                    objective="Finish safely",
                    status="active",
                    run_state="running",
                    revision=3,
                    last_run_id="run-1",
                    token_budget=1_000,
                    tokens_used=700,
                    cost_budget_microusd=1_000,
                    cost_used_microusd=100,
                    time_budget_seconds=100,
                    time_used_seconds=10,
                )
            )
            db.add(
                GoalRun(
                    id="run-1",
                    goal_id="goal",
                    ordinal=1,
                    goal_revision=3,
                    idempotency_key="goal:run-1",
                    trigger="initial",
                    status="running",
                )
            )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_durable_execution_gate_rejects_stale_or_paused_runs(goal_factory) -> None:
    current = await read_goal_execution_gate(
        goal_factory,
        session_id="session",
        goal_id="goal",
        goal_run_id="run-1",
    )
    assert current.allowed is True
    assert current.revision == 3

    stale = await read_goal_execution_gate(
        goal_factory,
        session_id="session",
        goal_id="goal",
        goal_run_id="run-old",
    )
    assert stale.allowed is False
    assert "newer GoalRun" in (stale.reason or "")

    async with goal_factory() as db:
        async with db.begin():
            goal = await db.get(SessionGoal, "goal")
            assert goal is not None
            goal.status = "paused"
            goal.run_state = "idle"
    paused = await read_goal_execution_gate(
        goal_factory,
        session_id="session",
        goal_id="goal",
        goal_run_id="run-1",
    )
    assert paused.allowed is False
    assert paused.status == "paused"


@pytest.mark.asyncio
async def test_durable_budget_gate_warns_then_hard_stops(goal_factory) -> None:
    warning = await read_goal_budget_gate(
        goal_factory,
        session_id="session",
        goal_id="goal",
        local_tokens_used=150,
        warning_ratio=0.8,
    )
    assert warning.allowed is True
    assert warning.warning is True
    assert warning.token_remaining == 150

    stopped = await read_goal_budget_gate(
        goal_factory,
        session_id="session",
        goal_id="goal",
        local_tokens_used=300,
        warning_ratio=0.8,
    )
    assert stopped.allowed is False
    assert stopped.reason_code == "token_budget"
    assert stopped.token_remaining == 0


@pytest.mark.asyncio
async def test_waiting_user_state_is_durable_and_does_not_change_revision(
    goal_factory,
) -> None:
    await set_goal_waiting_user(
        goal_factory,
        session_id="session",
        goal_id="goal",
        goal_run_id="run-1",
        waiting=True,
        blocker_code="permission_required",
        blocker_message="Permission required for write",
    )
    async with goal_factory() as db:
        goal = await db.get(SessionGoal, "goal")
        run = await db.get(GoalRun, "run-1")
    assert goal is not None and run is not None
    assert goal.run_state == "waiting_user"
    assert goal.revision == 3
    assert run.status == "waiting_user"

    await set_goal_waiting_user(
        goal_factory,
        session_id="session",
        goal_id="goal",
        goal_run_id="run-1",
        waiting=False,
    )
    async with goal_factory() as db:
        goal = await db.get(SessionGoal, "goal")
        run = await db.get(GoalRun, "run-1")
    assert goal is not None and run is not None
    assert goal.run_state == "running"
    assert run.status == "running"


@pytest.mark.asyncio
async def test_denied_interaction_blocks_goal_but_defers_run_accounting(goal_factory) -> None:
    await block_goal_for_user_action(
        goal_factory,
        session_id="session",
        goal_id="goal",
        goal_run_id="run-1",
        blocker_code="permission_denied",
        blocker_message="Permission denied for write",
    )
    async with goal_factory() as db:
        goal = await db.get(SessionGoal, "goal")
        run = await db.get(GoalRun, "run-1")
    assert goal is not None and run is not None
    assert goal.status == "blocked"
    assert goal.run_state == "idle"
    assert goal.revision == 4
    assert goal.blocker_code == "permission_denied"
    assert run.status == "running"
    assert run.time_finished is None


def test_goal_execution_gate_is_immutable() -> None:
    gate = GoalExecutionGate(
        allowed=False,
        goal_id="goal",
        status="paused",
        run_state="pausing",
        revision=4,
        reason="Goal status is paused",
    )
    with pytest.raises((AttributeError, TypeError)):
        gate.allowed = True  # type: ignore[misc]


def test_budget_remaining_is_cumulative_and_none_means_unlimited_dimension() -> None:
    assert _remaining(10_000, 2_000, 500) == 7_500
    assert _remaining(None, 2_000, 500) is None
    gate = GoalBudgetGate(
        allowed=False,
        warning=True,
        reason_code="token_budget",
        token_remaining=0,
        cost_remaining_microusd=1,
        time_remaining_seconds=1,
    )
    assert gate.reason_code == "token_budget"


@pytest.mark.asyncio
async def test_executor_rechecks_gate_before_starting_side_effect() -> None:
    calls = 0

    class SideEffectTool(ToolDefinition):
        @property
        def id(self) -> str:
            return "side_effect"

        @property
        def description(self) -> str:
            return "test"

        def parameters_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, args, ctx) -> ToolResult:
            nonlocal calls
            calls += 1
            return ToolResult(output="ran")

    ctx = ToolContext(
        session_id="session",
        message_id="message",
        agent=object(),  # type: ignore[arg-type]
        call_id="call",
        _execution_guard_fn=lambda: _deny(),
    )
    result = await _execute_single(
        ToolCallInfo(
            index=0,
            tool=SideEffectTool(),
            tool_name="side_effect",
            tool_args={},
            call_id="call",
            ctx=ctx,
        )
    )

    assert calls == 0
    assert isinstance(result.error, RuntimeError)
    assert "paused" in str(result.error)
