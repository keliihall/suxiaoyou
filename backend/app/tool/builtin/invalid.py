"""Invalid tool fallback — catches unrecognized tool calls."""

from __future__ import annotations

from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext


class InvalidTool(ToolDefinition):
    """Fallback tool for unrecognized tool calls.

    Returns an error message telling the LLM the tool doesn't exist.
    This is part of tool call repair (OpenCode pattern).
    """

    @property
    def id(self) -> str:
        return "invalid"

    @property
    def description(self) -> str:
        return "Fallback for unrecognized tool calls"

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The unrecognized tool name"},
            },
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("name", "unknown")
        return ToolResult(
            error=ctx.tr(f"工具“{name}”不可用。请检查工具名称后重试。", f'Tool "{name}" is unavailable. Check the tool name and try again.'),
        )
