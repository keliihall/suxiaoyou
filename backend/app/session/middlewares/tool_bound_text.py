"""Keep tool-bound model narration out of the normal assistant transcript.

Some providers emit planning or status prose as ordinary text immediately
before a tool call.  That text is neither the final answer nor durable user
content; showing it inline duplicates the activity timeline and can expose
provider-language implementation chatter.  A tool-using step always receives
a follow-up model step, so the final user-facing answer remains available.
"""

from __future__ import annotations

from typing import Any

from app.session.middleware import Middleware, MiddlewareContext


class ToolBoundTextMiddleware(Middleware):
    """Suppress ordinary response text on steps that dispatch tools."""

    async def after_llm_response(
        self,
        text: str,
        tool_calls: list[dict[str, Any]],
        ctx: MiddlewareContext,
    ) -> tuple[str, list[dict[str, Any]]]:
        if not tool_calls or not text.strip():
            return text, tool_calls

        # Record only a count for diagnostics.  Never copy provider narration
        # into logs or other user-visible metadata.
        ctx.extra["suppressed_tool_bound_text_chars"] = len(text)
        return "", tool_calls


__all__ = ["ToolBoundTextMiddleware"]
