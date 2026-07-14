from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.session.processor import (
    SessionProcessor,
    _permission_arguments_for_event,
    _permission_decision_from_response,
    _permission_message,
)
from app.session import processor as processor_module
from app.agent.agent import AgentRegistry
from app.agent.permission import evaluate, parse_permission_snapshot
from app.models.session import Session
from app.schemas.chat import PromptRequest
from app.schemas.provider import ModelInfo
from app.session.manager import create_session
from app.session.prompt import SessionPrompt
from app.streaming.manager import GenerationJob
from app.streaming.events import AGENT_ERROR, TOOL_ERROR
from app.schemas.agent import PermissionRule, Ruleset


def test_permission_arguments_redact_secret_like_keys() -> None:
    args, truncated = _permission_arguments_for_event({
        "command": "curl https://example.test",
        "api_key": "sk-test-secret",
        "nested": {"Authorization": "Bearer secret"},
    })

    assert truncated is False
    assert args["command"] == "curl https://example.test"
    assert args["api_key"] == "[redacted]"
    assert args["nested"] == {"Authorization": "[redacted]"}


def test_permission_arguments_truncate_large_values() -> None:
    args, truncated = _permission_arguments_for_event({
        "file_path": "report.md",
        "content": "x" * 25_000,
    })

    assert truncated is True
    assert args["file_path"] == "report.md"
    assert str(args["content"]).endswith("[permission preview truncated]")


def test_permission_message_shows_bash_command() -> None:
    message = _permission_message(
        "bash",
        {"command": "npm run preflight:ui"},
        truncated=False,
    )

    assert "shell command" in message
    assert "npm run preflight:ui" in message


def test_permission_message_shows_file_target_and_truncation() -> None:
    message = _permission_message(
        "write",
        {"file_path": "docs/launch.md"},
        truncated=True,
    )

    assert "docs/launch.md" in message
    assert "truncated" in message


def test_permission_decision_accepts_legacy_bool() -> None:
    assert _permission_decision_from_response(True) == {"allowed": True, "remember": False}
    assert _permission_decision_from_response(False) == {"allowed": False, "remember": False}


def test_permission_decision_accepts_remember_payload() -> None:
    assert _permission_decision_from_response({"allowed": True, "remember": True}) == {
        "allowed": True,
        "remember": True,
    }
    assert _permission_decision_from_response({"allowed": False, "remember": True}) == {
        "allowed": False,
        "remember": True,
    }


class _Provider:
    id = "test-provider"


class _ProviderRegistry:
    def __init__(self) -> None:
        self.provider = _Provider()
        self.model = ModelInfo(
            id="test-model",
            name="Test Model",
            provider_id=self.provider.id,
        )

    def resolve_model(self, _model_id: str, _provider_id: str | None = None):
        return self.provider, self.model

    async def refresh_models(self):
        return {}


class _ToolRegistry:
    pass


async def _setup_prompt(session_factory, request: PromptRequest) -> SessionPrompt:
    prompt = SessionPrompt(
        job=GenerationJob(stream_id="stream-test", session_id=request.session_id),
        request=request,
        session_factory=session_factory,
        provider_registry=_ProviderRegistry(),
        agent_registry=AgentRegistry(),
        tool_registry=_ToolRegistry(),
    )
    await prompt._setup()
    return prompt


@pytest.mark.asyncio
async def test_prompt_ignores_historical_session_permissions(session_factory) -> None:
    async with session_factory() as db:
        async with db.begin():
            session = await create_session(
                db,
                id="session-with-hidden-allow",
            )
            session.permission = [{"action": "allow", "permission": "bash", "pattern": "*"}]

    prompt = await _setup_prompt(
        session_factory,
        PromptRequest(
            session_id="session-with-hidden-allow",
            text="run a command",
            model="test-model",
        ),
    )

    assert evaluate("bash", "*", prompt.merged_permissions) == "ask"


@pytest.mark.asyncio
async def test_prompt_uses_request_permission_rules(session_factory) -> None:
    prompt = await _setup_prompt(
        session_factory,
        PromptRequest(
            session_id="session-with-request-allow",
            text="run a command",
            model="test-model",
            permission_rules=[
                {"action": "allow", "permission": "bash", "pattern": "*"},
            ],
        ),
    )

    assert evaluate("bash", "*", prompt.merged_permissions) == "allow"

    async with session_factory() as db:
        session = await db.get(Session, "session-with-request-allow")
    snapshot = parse_permission_snapshot(session.permission_snapshot)
    assert snapshot is not None
    assert evaluate("bash", "*", snapshot) == "allow"


@pytest.mark.asyncio
async def test_headless_ask_fails_terminally_and_blocks_later_tool_calls(monkeypatch) -> None:
    class _Tool:
        def __init__(self, tool_id: str) -> None:
            self.id = tool_id

    class _Registry:
        def get(self, name: str):
            return {"bash": _Tool("bash"), "read": _Tool("read")}.get(name)

    job = GenerationJob("headless-stream", "headless-session")
    job.interactive = False
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        tool_registry=_Registry(),
        merged_permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="bash"),
        ]),
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "ask-call",
        "name": "bash",
        "arguments": {"command": "touch must-not-exist"},
    }))

    assert processor._exec_blocked is True
    assert processor.finish_reason == "error"
    assert processor._streaming_executor.has_submissions is False
    assert any(event.event == TOOL_ERROR for event in job.events)
    assert any(event.event == AGENT_ERROR for event in job.events)

    # This is the exact guard used while consuming subsequent chunks from the
    # same model response. No later allow call may be submitted after the ask.
    if not processor._exec_blocked:
        await processor._handle_tool_call_chunk(SimpleNamespace(data={
            "id": "later-allow",
            "name": "read",
            "arguments": {"file_path": "allowed.txt"},
        }))
    assert processor._streaming_executor.has_submissions is False
    assert await processor._dispatch_tool_calls() == "stop"
