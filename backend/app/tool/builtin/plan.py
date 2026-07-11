"""Plan tool — switch between build and plan mode mid-conversation.

The processor detects the "switch_agent" metadata in the ToolResult
and changes the active agent for subsequent loop iterations.
"""

from __future__ import annotations

from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext


class PlanTool(ToolDefinition):
    """Switch between plan (read-only) and build (full access) modes."""

    @property
    def id(self) -> str:
        return "plan"

    @property
    def description(self) -> str:
        return (
            "Switch between plan and build modes. "
            "Use command='enter' to switch to plan mode (read-only analysis), "
            "or command='exit' to return to build mode (full tool access)."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["enter", "exit"],
                    "description": "'enter' to switch to read-only plan mode, 'exit' to return to build mode",
                },
            },
            "required": ["command"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]

        if command == "enter":
            # Guard: already in plan mode
            if ctx.agent.name == "plan":
                return ToolResult(error="已经处于计划模式。")

            return ToolResult(
                output=(
                    "已切换到计划模式。现在只能只读分析和规划。准备实施时，使用 plan(command='exit') 返回构建模式。"
                ),
                metadata={"switch_agent": "plan"},
            )
        else:  # exit
            # Guard: not in plan mode
            if ctx.agent.name != "plan":
                return ToolResult(error="当前不在计划模式。")

            return ToolResult(
                output="已切换到构建模式，完整工具权限已恢复。",
                metadata={"switch_agent": "build"},
            )
