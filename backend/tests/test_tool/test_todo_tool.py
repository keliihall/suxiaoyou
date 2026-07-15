"""Tests for app.tool.builtin.todo — TodoTool._build_result()."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.models.todo import Todo
from app.tool.builtin.todo import TodoTool, get_todo_reload_state, get_todos
from app.tool.context import ToolContext


class TestBuildResult:
    def test_summary_counts(self):
        todos = [
            {"content": "A", "status": "completed"},
            {"content": "B", "status": "in_progress"},
            {"content": "C", "status": "pending"},
        ]
        result = TodoTool._build_result(todos)
        assert "已完成 1/3" in result.output
        assert "1 个进行中" in result.output
        assert "1 个待处理" in result.output
        assert result.title == "待办清单"

    def test_all_completed(self):
        todos = [
            {"content": "A", "status": "completed"},
            {"content": "B", "status": "completed"},
        ]
        result = TodoTool._build_result(todos)
        assert "已完成 2/2" in result.output
        assert "待处理" not in result.output

    def test_empty_list(self):
        result = TodoTool._build_result([])
        assert "已完成 0/0" in result.output


def _context(*, goal_id: str | None = None) -> ToolContext:
    return ToolContext(
        session_id="session",
        message_id="message",
        agent=object(),  # type: ignore[arg-type]
        call_id="call",
        goal_id=goal_id,
    )


@pytest.mark.asyncio
async def test_missing_session_factory_returns_error_instead_of_false_success() -> None:
    result = await TodoTool().execute(
        {"todos": [{"content": "Must persist", "status": "pending"}]},
        _context(),
    )

    assert result.success is False
    assert "存储不可用" in (result.error or "")
    assert "todos" not in result.metadata


@pytest.mark.asyncio
async def test_success_uses_normalised_durable_readback(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'readback.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Readback"))

    ctx = _context()
    ctx._app_state = {"session_factory": factory}  # type: ignore[attr-defined]
    result = await TodoTool().execute(
        {"todos": [{"content": "Persist me", "status": "in_progress"}]},
        ctx,
    )

    expected = [{
        "content": "Persist me",
        "status": "in_progress",
        "activeForm": "",
    }]
    assert result.success is True
    assert result.metadata["todos"] == expected
    assert await get_todos("session", factory, goal_id=None) == expected
    await engine.dispose()


@pytest.mark.asyncio
async def test_readback_mismatch_returns_error(tmp_path, monkeypatch) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mismatch.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Mismatch"))

    async def mismatched_readback(*args, **kwargs):
        del args, kwargs
        return []

    monkeypatch.setattr("app.tool.builtin.todo.get_todos", mismatched_readback)
    ctx = _context()
    ctx._app_state = {"session_factory": factory}  # type: ignore[attr-defined]
    result = await TodoTool().execute(
        {"todos": [{"content": "Persist me", "status": "pending"}]},
        ctx,
    )

    assert result.success is False
    assert "验证失败" in (result.error or "")
    assert "todos" not in result.metadata
    await engine.dispose()


@pytest.mark.asyncio
async def test_goal_and_ordinary_todo_lists_do_not_overwrite_each_other(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'todo.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Todo isolation"))
            db.add(
                SessionGoal(
                    id="goal",
                    session_id="session",
                    objective="Finish",
                )
            )
            db.add_all(
                [
                    Todo(
                        id="normal-old",
                        session_id="session",
                        goal_id=None,
                        content="Normal old",
                    ),
                    Todo(
                        id="goal-old",
                        session_id="session",
                        goal_id="goal",
                        content="Goal old",
                    ),
                ]
            )

    goal_ctx = _context(goal_id="goal")
    goal_ctx._app_state = {"session_factory": factory}  # type: ignore[attr-defined]
    await TodoTool().execute(
        {"todos": [{"content": "Goal new", "status": "in_progress"}]},
        goal_ctx,
    )

    async with factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Todo).order_by(Todo.goal_id, Todo.content)
                )
            ).scalars().all()
        )
    assert {(row.goal_id, row.content) for row in rows} == {
        (None, "Normal old"),
        ("goal", "Goal new"),
    }

    normal_ctx = _context()
    normal_ctx._app_state = {"session_factory": factory}  # type: ignore[attr-defined]
    await TodoTool().execute(
        {"todos": [{"content": "Normal new", "status": "pending"}]},
        normal_ctx,
    )
    async with factory() as db:
        rows = list((await db.execute(select(Todo))).scalars().all())
    assert {(row.goal_id, row.content) for row in rows} == {
        (None, "Normal new"),
        ("goal", "Goal new"),
    }

    reload_state = await get_todo_reload_state("session", factory)
    assert reload_state == {
        "todos": [{
            "content": "Goal new",
            "status": "in_progress",
            "activeForm": "",
        }],
        "scope": "goal",
        "goal_id": "goal",
        "goal_status": "active",
        "groups": {
            "session": [{
                "content": "Normal new",
                "status": "pending",
                "activeForm": "",
            }],
            "goal": [{
                "content": "Goal new",
                "status": "in_progress",
                "activeForm": "",
            }],
        },
    }
    await engine.dispose()
