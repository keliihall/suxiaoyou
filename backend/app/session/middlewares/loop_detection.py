"""Middleware: two-stage loop detection for tool calls.

Detects repetitive tool calls using a sliding-window hash comparison.
Stage 1 (warn): inject a warning into tool output after warn_threshold hits.
Stage 2 (block): prevent tool execution after hard_limit hits.
"""

from __future__ import annotations

from typing import Any

from app.session.loop_detection import LoopCheckResult, loop_detector
from app.session.middleware import Middleware, MiddlewareContext, ToolAction


class LoopDetectionMiddleware(Middleware):
    """Two-stage warn-then-stop loop detection for tool calls."""

    async def before_tool_exec(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        ctx: MiddlewareContext,
    ) -> ToolAction:
        result: LoopCheckResult = loop_detector.check(
            ctx.session_id,
            tool_name,
            tool_args,
            language=ctx.job.language,
        )
        if result.action == "block":
            return ToolAction(
                action="block",
                message=result.message,
                code="loop_detected",
            )
        if result.action == "warn":
            # Return the warning to the processor so it stays attached to this
            # exact tool call even when several calls are finalized together.
            return ToolAction(action="warn", message=result.message)
        return ToolAction(action="allow")

    async def after_tool_exec(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        output: str,
        ctx: MiddlewareContext,
    ) -> str:
        # The processor appends the per-call ToolAction warning. Keeping this
        # hook as a no-op preserves the public middleware shape without using a
        # shared context slot that can mix warnings between concurrent tools.
        return output
