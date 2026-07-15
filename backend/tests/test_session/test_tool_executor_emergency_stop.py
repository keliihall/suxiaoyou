from __future__ import annotations

import asyncio

import pytest

from app.schemas.agent import AgentInfo
from app.session.tool_executor import StreamingToolExecutor, ToolCallInfo, _execute_single
from app.streaming.manager import StreamManager
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext


class _InFlightNetworkTool(ToolDefinition):
    def __init__(
        self,
        started: asyncio.Event,
        cancelled: asyncio.Event,
        *,
        concurrency_safe: bool = True,
    ) -> None:
        self.started = started
        self.cancelled = cancelled
        self.concurrency_safe = concurrency_safe

    @property
    def id(self) -> str:
        return "in_flight_network"

    @property
    def description(self) -> str:
        return "Test-only cancellable network operation"

    @property
    def is_concurrency_safe(self) -> bool:
        return self.concurrency_safe

    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _BoundaryTool(ToolDefinition):
    def __init__(self, started: asyncio.Event) -> None:
        self.started = started

    @property
    def id(self) -> str:
        return "boundary_tool"

    @property
    def description(self) -> str:
        return "Test-only execution-admission boundary"

    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.started.set()
        return ToolResult(output="started")


def _goal_tool_info(job, tool: ToolDefinition) -> ToolCallInfo:
    ctx = ToolContext(
        session_id=job.session_id,
        message_id="message",
        call_id="call",
        agent=AgentInfo(name="test", description="", mode="primary"),
        abort_event=job.abort_event,
        invocation_source="goal",
        goal_id=job.goal_id,
        goal_run_id=job.goal_run_id,
    )
    ctx._job = job  # type: ignore[attr-defined]
    return ToolCallInfo(
        index=0,
        tool=tool,
        tool_name=tool.id,
        tool_args={},
        call_id="call",
        ctx=ctx,
    )


@pytest.mark.asyncio
async def test_abort_all_cancels_and_awaits_an_in_flight_concurrent_tool() -> None:
    manager = StreamManager()
    job = manager.create_job("stream", "session")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    tool = _InFlightNetworkTool(started, cancelled)
    ctx = ToolContext(
        session_id="session",
        message_id="message",
        call_id="call",
        agent=AgentInfo(name="test", description="", mode="primary"),
        abort_event=job.abort_event,
    )
    executor = StreamingToolExecutor(
        job.abort_event,
        task_tracker=job.track_tool_task,
    )
    executor.submit(
        ToolCallInfo(
            index=0,
            tool=tool,
            tool_name=tool.id,
            tool_args={},
            call_id="call",
            ctx=ctx,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    count, quiesced = await manager.abort_all_and_wait(timeout=1)
    results = await executor.collect()

    assert count == 1
    assert quiesced is True
    assert cancelled.is_set()
    assert isinstance(results[0].error, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_abort_all_cancels_and_awaits_an_in_flight_exclusive_tool() -> None:
    manager = StreamManager()
    job = manager.create_job("exclusive-stream", "exclusive-session")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    tool = _InFlightNetworkTool(started, cancelled, concurrency_safe=False)
    ctx = ToolContext(
        session_id="exclusive-session",
        message_id="message",
        call_id="call",
        agent=AgentInfo(name="test", description="", mode="primary"),
        abort_event=job.abort_event,
    )
    executor = StreamingToolExecutor(
        job.abort_event,
        task_tracker=job.track_tool_task,
    )
    executor.submit(ToolCallInfo(
        index=0,
        tool=tool,
        tool_name=tool.id,
        tool_args={},
        call_id="call",
        ctx=ctx,
    ))
    collect_task = asyncio.create_task(executor.collect())
    await asyncio.wait_for(started.wait(), timeout=1)

    count, quiesced = await manager.abort_all_and_wait(timeout=1)
    results = await collect_task

    assert count == 1
    assert quiesced is True
    assert cancelled.is_set()
    assert isinstance(results[0].error, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_goal_pause_wins_before_final_tool_admission() -> None:
    manager = StreamManager()
    job = manager.create_job(
        "goal-tool-denied",
        "goal-tool-session",
        invocation_source="goal",
        goal_id="goal-1",
        goal_run_id="run-1",
    )
    started = asyncio.Event()
    info = _goal_tool_info(job, _BoundaryTool(started))

    await job.execution_admission_lock.acquire()
    execution = asyncio.create_task(_execute_single(info))
    await asyncio.sleep(0)
    job.close_execution_admission()
    job.execution_admission_lock.release()

    result = await execution
    assert started.is_set() is False
    assert isinstance(result.error, RuntimeError)
    assert "safe boundary" in str(result.error)


@pytest.mark.asyncio
async def test_goal_pause_linearizes_after_an_admitted_tool_has_started() -> None:
    manager = StreamManager()
    job = manager.create_job(
        "goal-tool-started",
        "goal-tool-session",
        invocation_source="goal",
        goal_id="goal-1",
        goal_run_id="run-1",
    )
    started = asyncio.Event()
    guard_entered = asyncio.Event()
    release_guard = asyncio.Event()
    pause_committed = asyncio.Event()
    info = _goal_tool_info(job, _BoundaryTool(started))

    async def guarded() -> tuple[bool, str | None]:
        guard_entered.set()
        await release_guard.wait()
        return True, None

    info.ctx._execution_guard_fn = guarded

    async def close_for_pause() -> None:
        async with job.execution_admission_lock:
            assert started.is_set() is True
            job.close_execution_admission()
            pause_committed.set()

    execution = asyncio.create_task(_execute_single(info))
    await guard_entered.wait()
    pause = asyncio.create_task(close_for_pause())
    await asyncio.sleep(0)
    assert pause_committed.is_set() is False

    release_guard.set()
    result = await execution
    await pause
    assert result.error is None
    assert result.result is not None and result.result.output == "started"
    assert pause_committed.is_set() is True
