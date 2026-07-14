"""Task tool (SubAgent) tests — recursion guard, validation."""

from unittest.mock import MagicMock

import pytest

from app.schemas.agent import AgentInfo
from app.schemas.chat import PromptRequest
from app.session.managed_workspace import managed_workspace_for_session
from app.session.manager import create_session
from app.session.prompt import _preflight_workspace_boundary
from app.streaming.events import SSEEvent
from app.tool.builtin.task import MAX_SUBTASK_DEPTH, TaskTool
from app.tool.context import ToolContext


def _make_ctx(depth: int = 0, *, workspace: str = "/workspace") -> ToolContext:
    ctx = ToolContext(
        session_id="test-session",
        message_id="test-msg",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="test-call",
        workspace=workspace,
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
    captured: dict[str, object] = {}

    async def fake_run_generation(job, request, **_kwargs):
        captured["job"] = job.language
        captured["request"] = request.language
        captured["workspace"] = request.workspace
        captured["permission_rules"] = request.permission_rules
        captured["permission_rules_authoritative"] = request._permission_rules_authoritative
        captured["abort_shared"] = job.abort_event is ctx.abort_event
        captured["interactive"] = job.interactive

    monkeypatch.setattr("app.session.processor.run_generation", fake_run_generation)
    ctx = _make_ctx(depth=0)
    ctx.language = "en"
    ctx.permission_rules = (
        {"action": "deny", "permission": "bash", "pattern": "*"},
        {"action": "allow", "permission": "read", "pattern": "*"},
    )
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
    assert captured == {
        "job": "en",
        "request": "en",
        "workspace": "/workspace",
        "permission_rules": [
            {"action": "deny", "permission": "bash", "pattern": "*"},
            {"action": "allow", "permission": "read", "pattern": "*"},
        ],
        "permission_rules_authoritative": True,
        "abort_shared": True,
        "interactive": False,
    }
    assert result.title == "Subtask (explore): locale propagation"

    from app.session.manager import get_session

    async with session_factory() as db:
        child = await get_session(db, result.metadata["task_id"])
    assert child is not None
    assert child.directory == "/workspace"


@pytest.mark.asyncio
async def test_subtask_requires_parent_workspace():
    ctx = _make_ctx(workspace="")
    result = await TaskTool().execute(
        {"description": "unsafe child", "prompt": "run"},
        ctx,
    )
    assert "selected workspace" in (result.error or "")


@pytest.mark.asyncio
async def test_subtask_agent_error_is_a_failed_tool_result(
    session_factory,
    monkeypatch,
):
    async def fake_run_generation(job, _request, **_kwargs):
        job.publish(SSEEvent("agent-error", {"error_message": "child permission failed"}))

    monkeypatch.setattr("app.session.processor.run_generation", fake_run_generation)
    ctx = _make_ctx()
    ctx._app_state = {  # type: ignore[attr-defined]
        "session_factory": session_factory,
        "provider_registry": MagicMock(),
        "agent_registry": MagicMock(),
        "tool_registry": MagicMock(),
    }

    result = await TaskTool().execute(
        {"description": "failing child", "prompt": "run denied command"},
        ctx,
    )

    assert not result.success
    assert "child permission failed" in (result.error or "")


@pytest.mark.asyncio
async def test_resumed_subtask_keeps_parent_permission_snapshot_authoritative(
    session_factory,
    monkeypatch,
):
    async with session_factory() as db:
        async with db.begin():
            child = await create_session(
                db,
                id="existing-child",
                parent_id="test-session",
                directory="/workspace",
            )
            child.permission = [
                {"action": "allow", "permission": "bash", "pattern": "*"},
            ]

    captured: dict[str, object] = {}

    async def fake_run_generation(job, request, **_kwargs):
        captured["interactive"] = job.interactive
        captured["rules"] = request.permission_rules
        captured["authoritative"] = request._permission_rules_authoritative
        job.publish(SSEEvent("text-delta", {"text": "resumed safely"}))

    monkeypatch.setattr("app.session.processor.run_generation", fake_run_generation)
    ctx = _make_ctx()
    ctx.permission_rules = (
        {"action": "deny", "permission": "bash", "pattern": "*"},
    )
    ctx._app_state = {  # type: ignore[attr-defined]
        "session_factory": session_factory,
        "provider_registry": MagicMock(),
        "agent_registry": MagicMock(),
        "tool_registry": MagicMock(),
    }

    result = await TaskTool().execute(
        {
            "description": "resume child",
            "prompt": "continue",
            "task_id": "existing-child",
        },
        ctx,
    )

    assert result.success
    assert result.metadata["resumed"] is True
    assert captured == {
        "interactive": False,
        "rules": [{"action": "deny", "permission": "bash", "pattern": "*"}],
        "authoritative": True,
    }


def test_external_prompt_cannot_enable_authoritative_permission_mode():
    request = PromptRequest.model_validate({
        "text": "untrusted",
        "permission_rules_authoritative": True,
        "_permission_rules_authoritative": True,
    })

    assert request._permission_rules_authoritative is False
    assert "permission_rules_authoritative" not in request.model_dump()


@pytest.mark.asyncio
async def test_folderless_parent_task_child_inherits_exact_managed_workspace(
    session_factory,
    monkeypatch,
    tmp_path,
):
    private_root = tmp_path / "app-private"
    managed_root = private_root / "managed-workspaces"
    private_root.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private_root))
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(managed_root))
    parent_workspace = managed_workspace_for_session("test-session")

    async with session_factory() as db:
        async with db.begin():
            await create_session(db, id="test-session", directory=".")

    captured: dict[str, object] = {}

    async def fake_run_generation(job, request, **_kwargs):
        session, allowed, canonical = await _preflight_workspace_boundary(
            session_factory,
            job.session_id,
            request.workspace,
        )
        captured.update({
            "parent_id": session.parent_id,
            "allowed": allowed,
            "canonical": canonical,
        })
        job.publish(SSEEvent("text-delta", {"text": "managed child ok"}))

    monkeypatch.setattr("app.session.processor.run_generation", fake_run_generation)
    ctx = _make_ctx(workspace=str(parent_workspace))
    ctx._app_state = {  # type: ignore[attr-defined]
        "session_factory": session_factory,
        "provider_registry": MagicMock(),
        "agent_registry": MagicMock(),
        "tool_registry": MagicMock(),
    }

    result = await TaskTool().execute(
        {"description": "managed child", "prompt": "inspect managed input"},
        ctx,
    )

    assert result.success
    assert captured == {
        "parent_id": "test-session",
        "allowed": parent_workspace.resolve(),
        "canonical": str(parent_workspace.resolve()),
    }
