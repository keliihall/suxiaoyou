"""Task tool (SubAgent) tests — recursion guard, validation."""

from unittest.mock import MagicMock

import pytest

from app.schemas.agent import AgentInfo
from app.tool.builtin.task import MAX_SUBTASK_DEPTH, TaskTool
from app.tool.context import ToolContext


def _make_ctx(depth: int = 0) -> ToolContext:
    ctx = ToolContext(
        session_id="test-session",
        message_id="test-msg",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="test-call",
    )
    ctx._depth = depth  # type: ignore[attr-defined]
    return ctx


class TestTaskValidation:
    def test_valid_args(self):
        tool = TaskTool()
        assert tool.validate_args({
            "description": "Search code",
            "prompt": "Find all Python files",
        }) is None

    def test_missing_description(self):
        tool = TaskTool()
        error = tool.validate_args({"prompt": "do something"})
        assert error is not None
        assert "description" in error

    def test_missing_prompt(self):
        tool = TaskTool()
        error = tool.validate_args({"description": "test"})
        assert error is not None
        assert "prompt" in error

    def test_invalid_agent_enum(self):
        tool = TaskTool()
        error = tool.validate_args({
            "description": "test",
            "prompt": "do something",
            "agent": "nonexistent",
        })
        assert error is not None
        assert "enum" in error.lower() or "must be one of" in error.lower()


class TestRecursionGuard:
    @pytest.mark.asyncio
    async def test_depth_0_allowed(self):
        """Depth 0 should not trigger recursion guard."""
        tool = TaskTool()
        ctx = _make_ctx(depth=0)
        # Will fail because no _app_state, but should NOT fail due to depth
        result = await tool.execute({
            "description": "test",
            "prompt": "test",
        }, ctx)
        assert "nesting depth" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_max_depth_blocked(self):
        """At max depth, should be blocked."""
        tool = TaskTool()
        ctx = _make_ctx(depth=MAX_SUBTASK_DEPTH)
        result = await tool.execute({
            "description": "test",
            "prompt": "test",
        }, ctx)
        assert result.error is not None
        assert "nesting depth" in result.error

    @pytest.mark.asyncio
    async def test_over_max_depth_blocked(self):
        """Over max depth, should also be blocked."""
        tool = TaskTool()
        ctx = _make_ctx(depth=MAX_SUBTASK_DEPTH + 5)
        result = await tool.execute({
            "description": "test",
            "prompt": "test",
        }, ctx)
        assert result.error is not None
        assert "nesting depth" in result.error

    @pytest.mark.asyncio
    async def test_no_app_state_error(self):
        """Without app_state, should return error (not crash)."""
        tool = TaskTool()
        ctx = _make_ctx(depth=0)
        result = await tool.execute({
            "description": "test",
            "prompt": "test",
        }, ctx)
        assert result.error is not None
        assert "app state" in result.error


@pytest.mark.asyncio
async def test_child_generation_inherits_english_locale(session_factory, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_run_generation(job, request, **_kwargs):
        captured["job"] = job.language
        captured["request"] = request.language

    monkeypatch.setattr("app.session.processor.run_generation", fake_run_generation)
    ctx = _make_ctx(depth=0)
    ctx.language = "en"
    ctx._app_state = {  # type: ignore[attr-defined]
        "session_factory": session_factory,
        "provider_registry": MagicMock(),
        "agent_registry": MagicMock(),
        "tool_registry": MagicMock(),
    }

    result = await TaskTool().execute(
        {"description": "locale propagation", "prompt": "Inspect locale"},
        ctx,
    )

    assert result.success
    assert captured == {"job": "en", "request": "en"}
    assert result.title == "Subtask (explore): locale propagation"
