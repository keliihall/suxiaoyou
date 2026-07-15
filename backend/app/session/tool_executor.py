"""Streaming tool executor — inspired by Claude Code's StreamingToolExecutor.

Key behavior:
  - Tools can be submitted during LLM streaming (before streaming completes)
  - Concurrent-safe tools start executing immediately in background tasks
  - Exclusive tools are queued and run sequentially after concurrent ones
  - Results are collected in submission order after streaming completes

This overlaps LLM network latency with tool I/O — the core performance
insight from Claude Code's architecture.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext

logger = logging.getLogger(__name__)


# Bash errors cancel sibling concurrent tools (inspired by Claude Code).
# Bash commands often form implicit dependency chains — if one fails,
# continuing siblings is pointless and can cause confusing cascading errors.
SIBLING_ABORT_TOOLS = frozenset({"bash"})


@dataclass
class ToolCallInfo:
    """A single tool call to be executed."""

    index: int  # Submission order
    tool: ToolDefinition
    tool_name: str
    tool_args: dict[str, Any]
    call_id: str
    ctx: ToolContext
    timeout: float = 300.0


@dataclass
class ToolExecutionResult:
    """Result of a single tool execution."""

    index: int
    tool_name: str
    call_id: str
    tool_args: dict[str, Any]
    result: ToolResult | None = None
    error: Exception | None = None
    timed_out: bool = False
    aborted_by_sibling: bool = False


async def _execute_single(info: ToolCallInfo) -> ToolExecutionResult:
    """Execute a single tool call with timeout and error handling."""
    if info.ctx.is_aborted:
        return ToolExecutionResult(
            index=info.index, tool_name=info.tool_name,
            call_id=info.call_id, tool_args=info.tool_args,
            error=asyncio.CancelledError("Aborted"),
        )
    tool_task: asyncio.Task[ToolResult] | None = None
    try:
        job = getattr(info.ctx, "_job", None)
        if job is not None and getattr(job, "goal_id", None) is not None:
            async with job.execution_admission_lock:
                if not job.execution_admission_open:
                    return ToolExecutionResult(
                        index=info.index,
                        tool_name=info.tool_name,
                        call_id=info.call_id,
                        tool_args=info.tool_args,
                        error=RuntimeError("Execution stopped at a safe boundary"),
                    )
                allowed, reason = await info.ctx.execution_allowed()
                if not allowed:
                    return ToolExecutionResult(
                        index=info.index,
                        tool_name=info.tool_name,
                        call_id=info.call_id,
                        tool_args=info.tool_args,
                        error=RuntimeError(reason or "Execution stopped at a safe boundary"),
                    )
                tool_task = asyncio.create_task(
                    info.tool(info.tool_args, info.ctx),
                    name=f"admitted-tool-{info.tool_name}-{info.call_id[:8]}",
                )
                # Let the admitted coroutine enter the tool while the control
                # lock is held. A pause then linearizes after an already-started
                # tool, never between its final gate and its first instruction.
                await asyncio.sleep(0)
        else:
            allowed, reason = await info.ctx.execution_allowed()
            if not allowed:
                return ToolExecutionResult(
                    index=info.index,
                    tool_name=info.tool_name,
                    call_id=info.call_id,
                    tool_args=info.tool_args,
                    error=RuntimeError(reason or "Execution stopped at a safe boundary"),
                )
            tool_task = asyncio.create_task(
                info.tool(info.tool_args, info.ctx),
                name=f"tool-body-{info.tool_name}-{info.call_id[:8]}",
            )
        result = await asyncio.wait_for(tool_task, timeout=info.timeout)
        return ToolExecutionResult(
            index=info.index, tool_name=info.tool_name,
            call_id=info.call_id, tool_args=info.tool_args,
            result=result,
        )
    except asyncio.TimeoutError:
        return ToolExecutionResult(
            index=info.index, tool_name=info.tool_name,
            call_id=info.call_id, tool_args=info.tool_args,
            timed_out=True,
        )
    except Exception as e:
        return ToolExecutionResult(
            index=info.index, tool_name=info.tool_name,
            call_id=info.call_id, tool_args=info.tool_args,
            error=e,
        )
    finally:
        if tool_task is not None and not tool_task.done():
            tool_task.cancel()
            await asyncio.gather(tool_task, return_exceptions=True)


class StreamingToolExecutor:
    """Manages tool execution during and after LLM streaming.

    Usage:
        executor = StreamingToolExecutor(abort_event)

        # During streaming — called each time a tool-call chunk arrives:
        executor.submit(tool_call_info)

        # After streaming completes — wait for all results:
        results = await executor.collect()
    """

    def __init__(
        self,
        abort_event: asyncio.Event,
        *,
        task_tracker: Callable[[asyncio.Task[Any]], None] | None = None,
    ) -> None:
        self._abort = abort_event
        self._task_tracker = task_tracker
        self._concurrent_tasks: list[tuple[ToolCallInfo, asyncio.Task]] = []
        self._exclusive_queue: list[ToolCallInfo] = []
        self._active_tasks: set[asyncio.Task[ToolExecutionResult]] = set()
        self._results: dict[int, ToolExecutionResult] = {}
        self._submission_order: list[int] = []
        # Sibling abort: set when a bash tool errors, cancels other concurrent tasks
        self._sibling_errored = False
        self._sibling_error_desc = ""

    def submit(self, info: ToolCallInfo) -> None:
        """Submit a tool for execution.

        Concurrent-safe tools start immediately as background tasks.
        Exclusive tools are queued for sequential execution after streaming.
        """
        self._submission_order.append(info.index)

        if info.tool.is_concurrency_safe:
            task = self._start_task(info)
            self._concurrent_tasks.append((info, task))
            logger.info(
                "Started concurrent tool %s (call_id=%s) during streaming",
                info.tool_name, info.call_id[:8],
            )
        else:
            self._exclusive_queue.append(info)
            logger.debug(
                "Queued exclusive tool %s (call_id=%s) for post-stream execution",
                info.tool_name, info.call_id[:8],
            )

    async def collect(self) -> list[ToolExecutionResult]:
        """Wait for all submitted tools to complete and return results in order.

        1. Await all concurrent background tasks (with sibling abort)
        2. Execute exclusive tools sequentially
        3. Return results sorted by submission order
        """
        # 1. Collect concurrent results
        for info, task in self._concurrent_tasks:
            try:
                result = await task
                self._results[result.index] = result

                # Sibling abort: if a bash tool errored, cancel remaining
                # concurrent tasks. Bash commands often have implicit dependency
                # chains — if one fails, continuing siblings is pointless.
                if (
                    result.error is not None
                    and result.tool_name in SIBLING_ABORT_TOOLS
                    and not self._sibling_errored
                ):
                    self._sibling_errored = True
                    _input_summary = str(info.tool_args.get("command", ""))[:40]
                    self._sibling_error_desc = (
                        f"{info.tool_name}({_input_summary})"
                        if _input_summary else info.tool_name
                    )
                    logger.info(
                        "Bash tool %s errored — cancelling sibling concurrent tasks",
                        info.call_id[:8],
                    )
                    self._cancel_remaining_concurrent(info.index)

            except asyncio.CancelledError:
                # Task was cancelled by sibling abort or external abort
                msg = (
                    f"Cancelled: parallel tool call {self._sibling_error_desc} errored"
                    if self._sibling_errored
                    else "Cancelled"
                )
                self._results[info.index] = ToolExecutionResult(
                    index=info.index, tool_name=info.tool_name,
                    call_id=info.call_id, tool_args=info.tool_args,
                    error=asyncio.CancelledError(msg),
                    aborted_by_sibling=self._sibling_errored,
                )
            except Exception as e:
                self._results[info.index] = ToolExecutionResult(
                    index=info.index, tool_name=info.tool_name,
                    call_id=info.call_id, tool_args=info.tool_args,
                    error=e,
                )

        # 2. Execute exclusive tools sequentially
        for info in self._exclusive_queue:
            if self._abort.is_set() or self._sibling_errored:
                msg = (
                    f"Cancelled: parallel tool call {self._sibling_error_desc} errored"
                    if self._sibling_errored
                    else "Aborted"
                )
                self._results[info.index] = ToolExecutionResult(
                    index=info.index, tool_name=info.tool_name,
                    call_id=info.call_id, tool_args=info.tool_args,
                    error=asyncio.CancelledError(msg),
                    aborted_by_sibling=self._sibling_errored,
                )
                continue

            task = self._start_task(info)
            try:
                result = await task
                self._results[result.index] = result
            except asyncio.CancelledError:
                self._results[info.index] = ToolExecutionResult(
                    index=info.index,
                    tool_name=info.tool_name,
                    call_id=info.call_id,
                    tool_args=info.tool_args,
                    error=asyncio.CancelledError("Aborted"),
                )

        # 3. Return in submission order
        return [
            self._results[idx]
            for idx in self._submission_order
            if idx in self._results
        ]

    def _cancel_remaining_concurrent(self, errored_index: int) -> None:
        """Cancel all concurrent tasks that haven't completed yet."""
        for info, task in self._concurrent_tasks:
            if info.index != errored_index and not task.done():
                task.cancel()
                logger.debug(
                    "Cancelled sibling tool %s (call_id=%s)",
                    info.tool_name, info.call_id[:8],
                )

    def _start_task(self, info: ToolCallInfo) -> asyncio.Task[ToolExecutionResult]:
        task = asyncio.create_task(
            _execute_single(info),
            name=f"tool-{info.tool_name}-{info.call_id[:8]}",
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        if self._task_tracker is not None:
            self._task_tracker(task)
        if self._abort.is_set() and not task.done():
            task.cancel()
        return task

    def cancel_all(self) -> None:
        """Cancel every running concurrent or exclusive tool task."""
        for task in list(self._active_tasks):
            if not task.done():
                task.cancel()

    @property
    def has_submissions(self) -> bool:
        return bool(self._submission_order)
