"""Task tool — SubAgent invocation.

Spawns a child session with its own agent loop, enabling:
  - Parallel exploration (explore subagent)
  - Code search delegation
  - Multi-step subtask execution (general subagent)

Improvements over initial implementation:
  - Sets parent_id on child session for proper hierarchy
  - Recursion depth guard (max 3 levels) prevents infinite nesting
  - Timeout prevents child sessions from running forever
  - Abort signal propagated from parent to child
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext

logger = logging.getLogger(__name__)

# Maximum nesting depth for subtasks to prevent infinite recursion
MAX_SUBTASK_DEPTH = 3

# Default timeout for subtask execution (seconds)
SUBTASK_TIMEOUT = 600.0


class TaskTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "Launch a specialized subagent to handle a complex subtask. "
            "Available agent types: 'explore' (fast codebase search), "
            "'general' (full access minus todo). "
            "The subagent runs its own agent loop and returns the result. "
            "Pass task_id from a previous result to resume an existing subtask session."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short description of the subtask (3-5 words)",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed instructions for the subagent",
                },
                "agent": {
                    "type": "string",
                    "description": "Subagent type: 'explore' or 'general'",
                    "default": "explore",
                    "enum": ["explore", "general"],
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional. Resume a previous subtask by passing the task_id from a prior result.",
                },
            },
            "required": ["description", "prompt"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        description = args["description"]
        prompt = args["prompt"]
        agent_name = args.get("agent", "explore")
        task_id = args.get("task_id")

        # Import here to avoid circular imports
        from app.agent.permission import (
            parse_permission_policy_baseline,
            parse_permission_snapshot,
        )
        from app.schemas.chat import PromptRequest
        from app.session.processor import run_generation
        from app.streaming.manager import GenerationJob
        from app.utils.id import generate_ulid

        # --- Recursion depth guard ---
        parent_depth = getattr(ctx, "_depth", 0)
        if parent_depth >= MAX_SUBTASK_DEPTH:
            return ToolResult(
                error=(
                    f"Maximum subtask nesting depth ({MAX_SUBTASK_DEPTH}) exceeded. "
                    "Complete the current task directly instead of delegating further."
                ),
            )

        if not ctx.workspace:
            return ToolResult(
                error="SubAgent requires the parent's selected workspace",
            )
        parent_workspace = str(Path(ctx.workspace).resolve())

        # Access app-level registries through the _app_state injected by processor
        app_state = getattr(ctx, "_app_state", None)
        if not app_state:
            return ToolResult(error="SubAgent not available: missing app state")

        session_factory = app_state["session_factory"]
        child_stream_id = generate_ulid()
        resuming = False

        # --- Resume existing session or create new one ---
        from app.session.manager import create_session, get_session
        if task_id:
            # Try to resume an existing child session
            async with session_factory() as db:
                existing = await get_session(db, task_id)
                existing_workspace = (
                    str(Path(existing.directory).resolve())
                    if existing and existing.directory and existing.directory != "."
                    else None
                )
                if (
                    existing
                    and existing.parent_id == ctx.session_id
                    and existing_workspace == parent_workspace
                ):
                    child_session_id = task_id
                    resuming = True
                    logger.info("Resuming subtask session %s", task_id)
                else:
                    logger.warning(
                        "task_id %s not found or execution context mismatch; creating new session",
                        task_id,
                    )
                    task_id = None  # Fall through to create new

        if not task_id:
            child_session_id = generate_ulid()
            async with session_factory() as db:
                async with db.begin():
                    await create_session(
                        db,
                        id=child_session_id,
                        parent_id=ctx.session_id,
                        directory=parent_workspace,
                        title=description,
                    )

        # Create a child job to capture the output
        child_job = GenerationJob(
            stream_id=child_stream_id,
            session_id=child_session_id,
            language=ctx.language,
            invocation_source=ctx.invocation_source,
            invocation_source_id=ctx.invocation_source_id,
        )
        parent_job = getattr(ctx, "_job", None)
        if parent_job is not None:
            child_job.inherit_runtime_context(parent_job)
            child_job.inherit_goal_context(parent_job)
        # The child is headless: it shares cancellation with its parent and
        # cannot auto-approve an inherited `ask` rule. Explicit parent `allow`
        # rules continue to work because they are copied into the request.
        child_job.abort_event = ctx.abort_event
        child_job.interactive = False
        # Propagate depth for nested recursion guard
        child_job._depth = parent_depth + 1

        # Build the child request
        child_request = PromptRequest(
            session_id=child_session_id,
            text=prompt,
            model=getattr(ctx, "_model_id", None),
            agent=agent_name,
            language=ctx.language,
            workspace=parent_workspace,
            permission_rules=[dict(rule) for rule in ctx.permission_rules],
        )
        child_request._permission_rules_authoritative = True
        if ctx.goal_id is not None and ctx.permission_snapshot is not None:
            trusted_ceiling = parse_permission_snapshot(
                ctx.permission_snapshot
            )
            if trusted_ceiling is not None:
                child_request._trusted_permission_ruleset = trusted_ceiling
                child_request._enforce_current_permission_ceiling = True
                child_request._goal_permission_baseline = (
                    parse_permission_policy_baseline(ctx.permission_snapshot)
                )

        # Publish subtask start to parent stream
        if ctx._publish_fn:
            ctx._publish_fn("subtask_start", {
                "session_id": child_session_id,
                "parent_id": ctx.session_id,
                "title": description,
                "agent": agent_name,
                "depth": parent_depth + 1,
                "resumed": resuming,
            })

        timed_out = False
        try:
            # Run with timeout + abort propagation
            await asyncio.wait_for(
                run_generation(
                    child_job,
                    child_request,
                    session_factory=session_factory,
                    provider_registry=app_state["provider_registry"],
                    agent_registry=app_state["agent_registry"],
                    tool_registry=app_state["tool_registry"],
                ),
                timeout=SUBTASK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            timed_out = True
            child_job.abort()
            logger.warning(
                "Subtask timed out after %.0fs: %s", SUBTASK_TIMEOUT, description,
            )
        except Exception as e:
            logger.exception("SubAgent error")
            return ToolResult(error=f"SubAgent failed: {e}")

        # Extract text output and key tool results from child events
        output_parts: list[str] = []
        error_parts: list[str] = []
        tool_results: list[str] = []
        for event in child_job.events:
            if event.event == "text-delta":
                output_parts.append(event.data.get("text", ""))
            elif event.event == "tool-result":
                # Capture tool results so the parent agent has visibility
                # into what the subagent actually found/did.
                tool_name = event.data.get("tool", "")
                tool_output = event.data.get("output", "")
                if tool_name and tool_output:
                    # Truncate individual results to avoid overwhelming the parent
                    if len(tool_output) > 2000:
                        tool_output = tool_output[:2000] + "... [truncated]"
                    tool_results.append(f"[{tool_name}] {tool_output}")
            elif event.event in {"agent-error", "error"}:
                error_parts.append(
                    str(
                        event.data.get("error_message")
                        or event.data.get("message")
                        or "error"
                    )
                )

        output = "".join(output_parts)

        # Append the most recent tool results (last 5) so the parent agent
        # can see what the subagent discovered, not just its text summary.
        if tool_results:
            recent_results = tool_results[-5:]
            output += ctx.tr("\n\n--- 关键工具结果 ---\n", "\n\n--- Key tool results ---\n")
            output += "\n\n".join(recent_results)

        result_metadata = {
            "task_id": child_session_id,
            "session_id": child_session_id,
            "parent_id": ctx.session_id,
            "agent": agent_name,
            "depth": parent_depth + 1,
            "resumed": resuming,
            "events": len(child_job.events),
            "tool_calls": len(tool_results),
        }

        if timed_out:
            result_metadata.update({
                "timeout": True,
                "timeout_seconds": SUBTASK_TIMEOUT,
            })
            if output.strip():
                # Preserve bounded diagnostic context without representing the
                # incomplete child response as a successful tool output.
                result_metadata["partial_output"] = output[:4000]
            return ToolResult(
                error=ctx.tr(
                    f"子任务在 {SUBTASK_TIMEOUT:g} 秒后超时",
                    f"Subtask timed out after {SUBTASK_TIMEOUT:g} seconds",
                ),
                metadata=result_metadata,
            )

        if error_parts:
            return ToolResult(
                error=ctx.tr("子任务失败：", "Subtask failed: ") + "; ".join(error_parts),
                metadata=result_metadata,
            )
        if not output.strip():
            output = ctx.tr("（子任务没有产生文本输出）", "(The subtask produced no text output)")

        return ToolResult(
            output=output,
            title=ctx.tr(f"子任务（{agent_name}）：{description}", f"Subtask ({agent_name}): {description}"),
            metadata=result_metadata,
        )
