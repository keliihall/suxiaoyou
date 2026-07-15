"""Todo tool — create and manage a task list for the session.

Persists todos to the database so they survive server restarts.
Each call replaces the entire todo list for the session (full replace strategy).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select

from app.i18n import localize
from app.models.session_goal import SessionGoal
from app.models.todo import Todo
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.utils.id import generate_ulid

logger = logging.getLogger(__name__)

_ALL_TODO_OWNERS = object()


def _normalise_todos(todos: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the exact public representation written to durable storage."""

    return [
        {
            "content": str(todo.get("content", "")),
            "status": str(todo.get("status", "pending")),
            "activeForm": str(todo.get("activeForm", "")),
        }
        for todo in todos
    ]


def _serialise_rows(rows: Any) -> list[dict[str, str]]:
    return [
        {
            "content": row.content,
            "status": row.status,
            "activeForm": row.active_form,
        }
        for row in rows
    ]


class TodoTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "todo"

    @property
    def description(self) -> str:
        return (
            "Track progress for multi-step tasks (3+ steps). "
            "The user sees updates in real-time.\n\n"
            "States: pending | in_progress (ONE only) | completed\n\n"
            "USAGE PATTERN:\n"
            "1. Create list: first task \"in_progress\", others \"pending\"\n"
            "2. After EACH task completes: call todo to update (mark done + start next)\n"
            "3. Never batch updates — call immediately after each step\n\n"
            "Fields:\n"
            "- content: what to do (\"Fix bug\")\n"
            "- activeForm: shown during work (\"Fixing bug\")\n"
            "- status: pending/in_progress/completed"
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {"type": "string"},
                        },
                        "required": ["content", "status"],
                    },
                    "description": "The complete todo list (replaces existing)",
                },
            },
            "required": ["todos"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos = _normalise_todos(args["todos"])

        # Access session_factory from app_state (injected by processor)
        app_state = getattr(ctx, "_app_state", None) or {}
        session_factory = app_state.get("session_factory")
        if session_factory is None:
            logger.error("TodoTool: no session_factory available")
            return ToolResult(error=ctx.tr(
                "Todo 存储不可用，待办清单未更新",
                "Todo storage is unavailable; the todo list was not updated",
            ))

        try:
            async with session_factory() as db:
                async with db.begin():
                    # Goal and ordinary-conversation Todo lists have independent
                    # ownership.  A side conversation while a Goal is paused must
                    # never erase the durable execution plan the Goal will resume.
                    delete_statement = delete(Todo).where(
                        Todo.session_id == ctx.session_id,
                    )
                    if ctx.goal_id is None:
                        delete_statement = delete_statement.where(
                            Todo.goal_id.is_(None),
                        )
                    else:
                        delete_statement = delete_statement.where(
                            Todo.goal_id == ctx.goal_id,
                        )
                    await db.execute(delete_statement)

                    # Insert new todos with position ordering.
                    for i, todo in enumerate(todos):
                        db.add(Todo(
                            id=generate_ulid(),
                            session_id=ctx.session_id,
                            goal_id=ctx.goal_id,
                            content=todo["content"],
                            status=todo["status"],
                            active_form=todo["activeForm"],
                            position=i,
                        ))

            # A successful transaction exit only tells us that commit did not
            # raise.  Read from a fresh session and compare the durable public
            # projection before telling the model/UI that the update succeeded.
            persisted = await get_todos(
                ctx.session_id,
                session_factory,
                goal_id=ctx.goal_id,
            )
        except Exception:
            logger.exception("TodoTool: durable update failed")
            return ToolResult(error=ctx.tr(
                "Todo 持久化失败，无法确认待办清单已更新",
                "Todo persistence failed; the update could not be verified",
            ))

        if persisted != todos:
            logger.error(
                "TodoTool: read-back mismatch for session=%s goal=%s",
                ctx.session_id,
                ctx.goal_id,
            )
            return ToolResult(error=ctx.tr(
                "Todo 持久化验证失败，待办清单状态不确定",
                "Todo persistence verification failed; list state is uncertain",
            ))

        return self._build_result(persisted, ctx)

    @staticmethod
    def _build_result(todos: list[dict[str, Any]], ctx: ToolContext | None = None) -> ToolResult:
        total = len(todos)
        completed = sum(1 for t in todos if t.get("status") == "completed")
        in_progress = sum(1 for t in todos if t.get("status") == "in_progress")
        pending = total - completed - in_progress

        tr = ctx.tr if ctx is not None else lambda zh, en: localize("zh", zh, en)
        summary = tr(
            f"待办清单已更新：已完成 {completed}/{total}",
            f"Todo list updated: {completed}/{total} completed",
        )
        if in_progress:
            summary += tr(f"，{in_progress} 个进行中", f", {in_progress} in progress")
        if pending:
            summary += tr(f"，{pending} 个待处理", f", {pending} pending")

        return ToolResult(
            output=summary,
            title=tr("待办清单", "Todo list"),
            metadata={"todos": todos},
        )


async def get_todos(
    session_id: str,
    session_factory: Any,
    *,
    goal_id: str | None | object = _ALL_TODO_OWNERS,
) -> list[dict[str, str]]:
    """Get todos from durable storage, optionally restricted to one owner.

    Omitting ``goal_id`` preserves the legacy all-rows behaviour for internal
    callers.  Passing ``None`` selects ordinary conversation todos; passing a
    Goal id selects only that Goal's execution plan.
    """

    async with session_factory() as db:
        stmt = (
            select(Todo)
            .where(Todo.session_id == session_id)
            .order_by(Todo.position, Todo.id)
        )
        if goal_id is not _ALL_TODO_OWNERS:
            if goal_id is None:
                stmt = stmt.where(Todo.goal_id.is_(None))
            else:
                stmt = stmt.where(Todo.goal_id == goal_id)
        result = await db.execute(stmt)
        rows = result.scalars().all()

    return _serialise_rows(rows)


async def get_todo_reload_state(
    session_id: str,
    session_factory: Any,
) -> dict[str, Any]:
    """Return an owner-safe reload projection for the workspace UI.

    A session has at most one persistent Goal.  When it exists, its Todo plan
    is the effective top-level list across active, paused, limited, blocked,
    and completed states; ordinary conversation todos remain available in the
    grouped extension.  With no Goal, the top-level list remains the ordinary
    list, matching the pre-Goal API contract.
    """

    async with session_factory() as db:
        goal = (
            await db.execute(
                select(SessionGoal).where(SessionGoal.session_id == session_id),
            )
        ).scalar_one_or_none()
        rows = list((await db.execute(
            select(Todo)
            .where(Todo.session_id == session_id)
            .order_by(Todo.position, Todo.id),
        )).scalars().all())

    ordinary = _serialise_rows(row for row in rows if row.goal_id is None)
    goal_todos = _serialise_rows(
        row for row in rows if goal is not None and row.goal_id == goal.id
    )
    selected = goal_todos if goal is not None else ordinary
    return {
        "todos": selected,
        "scope": "goal" if goal is not None else "session",
        "goal_id": goal.id if goal is not None else None,
        "goal_status": goal.status if goal is not None else None,
        "groups": {
            "session": ordinary,
            "goal": goal_todos,
        },
    }
