import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.session.processor import (
    SessionProcessor,
    _native_web_search_allowed,
    _permission_arguments_for_event,
    _permission_decision_from_response,
    _permission_metadata,
    _permission_message,
)
from app.session import processor as processor_module
from app.agent.agent import AgentRegistry
from app.agent.permission import evaluate, parse_permission_snapshot
from app.models.session import Session
from app.mcp.tool_wrapper import McpToolWrapper
from app.schemas.chat import PromptRequest
from app.schemas.provider import ModelInfo
from app.session.manager import create_session
from app.session.prompt import SessionPrompt, _merge_prompt_permission_layers
from app.streaming.manager import GenerationJob
from app.streaming.events import AGENT_ERROR, TOOL_ERROR
from app.schemas.agent import PermissionRule, Ruleset
from app.security.audit import AuditPersistenceError


def test_native_web_search_requires_enabled_security_control_and_permission() -> None:
    registry = SimpleNamespace(is_enabled=lambda tool_id: tool_id == "web_search")
    allowed = Ruleset(rules=[PermissionRule(action="allow", permission="*")])
    denied = Ruleset(rules=[PermissionRule(action="deny", permission="web_search")])
    requires_confirmation = Ruleset(rules=[
        PermissionRule(action="allow", permission="*"),
        PermissionRule(action="ask", permission="web_search"),
    ])

    assert _native_web_search_allowed(
        registry,
        allowed,
        quota_exhausted=False,
    ) is True
    assert _native_web_search_allowed(
        SimpleNamespace(is_enabled=lambda _tool_id: False),
        allowed,
        quota_exhausted=False,
    ) is False
    assert _native_web_search_allowed(
        registry,
        denied,
        quota_exhausted=False,
    ) is False
    assert _native_web_search_allowed(
        registry,
        requires_confirmation,
        quota_exhausted=False,
    ) is False
    assert _native_web_search_allowed(
        registry,
        allowed,
        quota_exhausted=False,
        invocation_source="channel",
    ) is False
    assert _native_web_search_allowed(
        registry,
        allowed,
        quota_exhausted=False,
        invocation_source="scheduler",
    ) is True


def test_native_web_search_respects_exhausted_quota() -> None:
    registry = SimpleNamespace(is_enabled=lambda _tool_id: True)
    allowed = Ruleset(rules=[PermissionRule(action="allow", permission="*")])

    assert _native_web_search_allowed(
        registry,
        allowed,
        quota_exhausted=True,
    ) is False


@pytest.mark.asyncio
async def test_native_search_consumes_quota_and_writes_redacted_audit(
    session_factory,
    monkeypatch,
) -> None:
    job = GenerationJob(
        "native-search-stream",
        "native-search-session",
        invocation_source="scheduler",
        invocation_source_id="task-1",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=session_factory,
        provider=SimpleNamespace(id="openai-subscription"),
        request=SimpleNamespace(language="en"),
        step=4,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    create_part = AsyncMock()
    update_part_data = AsyncMock()
    audit = AsyncMock()
    quota = SimpleNamespace(increment=AsyncMock())
    monkeypatch.setattr(processor_module, "create_part", create_part)
    monkeypatch.setattr(processor_module, "update_part_data", update_part_data)
    monkeypatch.setattr(processor_module, "record_security_event", audit)
    monkeypatch.setattr(processor_module, "_search_quota", quota)

    await processor._handle_web_search_start_chunk(SimpleNamespace(data={
        "id": "ws-redacted",
        "query": "private customer query",
    }))
    result_chunk = SimpleNamespace(data={
        "id": "ws-redacted",
        "query": "private customer query",
        "results": [{
            "title": "Sensitive result",
            "url": "https://secret.example/result",
            "snippet": "private source text",
        }],
    })
    await processor._handle_web_search_result_chunk(result_chunk)
    # A repeated provider event must not consume quota or duplicate the audit.
    await processor._handle_web_search_result_chunk(result_chunk)

    quota.increment.assert_awaited_once_with(charged=False)
    assert [call.kwargs["outcome"] for call in audit.await_args_list] == [
        "started",
        "success",
    ]
    for call in audit.await_args_list:
        assert call.kwargs["source_kind"] == "provider"
        assert call.kwargs["capability"] == "web_search"
        assert call.kwargs["invocation_source_kind"] == "scheduler"
        assert call.kwargs["invocation_source_id"] == "task-1"
        assert call.kwargs["details"] == {
            "native": True,
            "step": 4,
            "invocation_source": "scheduler",
        }
        serialized = str(call.kwargs)
        assert "private customer query" not in serialized
        assert "secret.example" not in serialized


@pytest.mark.asyncio
async def test_provider_inference_lifecycle_audit_excludes_prompt_content(
    session_factory,
    monkeypatch,
) -> None:
    job = GenerationJob(
        "provider-stream",
        "provider-session",
        invocation_source="scheduler",
        invocation_source_id="task-2",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=session_factory,
        provider=SimpleNamespace(id="test-provider"),
        model_id="test-model",
        system_prompt="system secret",
        agent=SimpleNamespace(),
        tool_registry=SimpleNamespace(),
        discovered_tools=set(),
        request=SimpleNamespace(format=None),
        step=7,
    )
    processor = SessionProcessor(
        prompt,
        [{"role": "user", "content": "private prompt text"}],
        "assistant-message",
    )
    processor._init_step_state()
    processor._build_stream_args = AsyncMock(return_value=(None, 128, None, False))
    processor._check_vision_blocked = AsyncMock(return_value=None)
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "record_security_event", audit)

    async def fake_stream_llm(*_args, **_kwargs):
        yield SimpleNamespace(type="text-delta", data={"text": "ok"})
        yield SimpleNamespace(type="finish", data={"reason": "stop"})

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)

    assert await processor._stream_llm_with_retry() is None
    assert [call.kwargs["outcome"] for call in audit.await_args_list] == [
        "started",
        "success",
    ]
    for call in audit.await_args_list:
        assert call.kwargs["source_kind"] == "provider"
        assert call.kwargs["source_id"] == "test-provider"
        assert call.kwargs["capability"] == "model_inference"
        assert call.kwargs["session_id"] == "provider-session"
        assert call.kwargs["invocation_source_kind"] == "scheduler"
        assert call.kwargs["invocation_source_id"] == "task-2"
        assert call.kwargs["details"] == {
            "step": 7,
            "attempt": 1,
            "invocation_source": "scheduler",
        }
        serialized = str(call.kwargs)
        assert "private prompt text" not in serialized
        assert "system secret" not in serialized
        assert "test-model" not in serialized
    assert audit.await_args_list[0].kwargs["required"] is True
    assert audit.await_args_list[1].kwargs["required"] is False


@pytest.mark.asyncio
async def test_goal_pause_wins_before_final_provider_admission(
    monkeypatch,
) -> None:
    job = GenerationJob(
        "goal-provider-denied",
        "goal-provider-session",
        invocation_source="goal",
        goal_id="goal-1",
        goal_run_id="run-1",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        provider=SimpleNamespace(id="test-provider"),
        model_id="test-model",
        system_prompt="system",
        agent=SimpleNamespace(),
        tool_registry=SimpleNamespace(),
        discovered_tools=set(),
        request=SimpleNamespace(format=None),
        step=1,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    processor._build_stream_args = AsyncMock(return_value=(None, 128, None, False))
    processor._check_vision_blocked = AsyncMock(return_value=None)
    processor._goal_provider_admission_allowed = AsyncMock(return_value=True)
    provider_started = False

    async def fake_stream_llm(*_args, **_kwargs):
        nonlocal provider_started
        provider_started = True
        yield SimpleNamespace(type="finish", data={"reason": "stop"})

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(processor_module, "record_security_event", AsyncMock())

    await job.execution_admission_lock.acquire()
    task = asyncio.create_task(processor._stream_llm_with_retry())
    await asyncio.sleep(0)
    # The control plane closes the in-memory gate before its durable pause.
    job.close_execution_admission()
    job.execution_admission_lock.release()

    assert await task == "stop"
    assert provider_started is False
    processor._goal_provider_admission_allowed.assert_not_awaited()


@pytest.mark.asyncio
async def test_goal_pause_waits_when_provider_has_already_started(
    monkeypatch,
) -> None:
    job = GenerationJob(
        "goal-provider-started",
        "goal-provider-session",
        invocation_source="goal",
        goal_id="goal-1",
        goal_run_id="run-1",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        provider=SimpleNamespace(id="test-provider"),
        model_id="test-model",
        system_prompt="system",
        agent=SimpleNamespace(),
        tool_registry=SimpleNamespace(),
        discovered_tools=set(),
        request=SimpleNamespace(format=None),
        step=1,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    processor._build_stream_args = AsyncMock(return_value=(None, 128, None, False))
    processor._check_vision_blocked = AsyncMock(return_value=None)
    processor._goal_provider_admission_allowed = AsyncMock(return_value=True)
    provider_entered = asyncio.Event()
    release_first_chunk = asyncio.Event()
    pause_committed = asyncio.Event()

    async def fake_stream_llm(*_args, **_kwargs):
        provider_entered.set()
        await release_first_chunk.wait()
        yield SimpleNamespace(type="text-delta", data={"text": "started"})
        yield SimpleNamespace(type="finish", data={"reason": "stop"})

    async def close_for_pause() -> None:
        async with job.execution_admission_lock:
            job.close_execution_admission()
            pause_committed.set()

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(processor_module, "record_security_event", AsyncMock())

    provider_task = asyncio.create_task(processor._stream_llm_with_retry())
    await provider_entered.wait()
    pause_task = asyncio.create_task(close_for_pause())
    await asyncio.sleep(0)
    assert pause_committed.is_set() is False

    release_first_chunk.set()
    await pause_task
    assert pause_committed.is_set() is True
    assert await provider_task is None


@pytest.mark.asyncio
async def test_custom_connector_provenance_reaches_security_audit(
    monkeypatch,
) -> None:
    client = SimpleNamespace(
        name="user-connector",
        connector_provenance="custom",
        tool_id=lambda name: f"user-connector_{name}",
        tool_requires_approval=lambda _name: False,
    )
    tool = McpToolWrapper(
        client,
        SimpleNamespace(
            name="read_remote",
            description="Read remote data",
            inputSchema={"type": "object"},
        ),
    )
    job = GenerationJob(
        "custom-connector-audit",
        "custom-connector-session",
        invocation_source="desktop",
    )
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "record_security_event", audit)

    await processor_module._audit_tool_event(
        object(),
        tool=tool,
        job=job,
        call_id="connector-call",
        decision="allow",
        outcome="started",
        interactive=True,
        required=True,
    )

    kwargs = audit.await_args.kwargs
    assert kwargs["source_kind"] == "connector_custom"
    assert kwargs["source_id"] == "user-connector"
    assert kwargs["details"]["connector_provenance"] == "custom"
    assert "custom_connector" in kwargs["details"]["capabilities"]
    assert kwargs["required"] is True


@pytest.mark.asyncio
async def test_subscription_search_ask_keeps_custom_tool_but_deny_excludes_it(
    monkeypatch,
) -> None:
    tool_registry = SimpleNamespace(is_enabled=lambda _tool_id: True)
    prompt = SimpleNamespace(
        job=GenerationJob(
            "subscription-stream",
            "subscription-session",
            invocation_source="desktop",
        ),
        request=SimpleNamespace(reasoning=None),
        model_info=None,
        tool_registry=tool_registry,
        provider=SimpleNamespace(id="openai-subscription"),
        merged_permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="web_search"),
        ]),
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    quota = SimpleNamespace(get_quota=AsyncMock(return_value=(0, False)))
    monkeypatch.setattr(processor_module, "_search_quota", quota)

    _reasoning, _tokens, excluded, native = await processor._build_stream_args()
    assert native is False
    assert not excluded or "web_search" not in excluded

    prompt.merged_permissions = Ruleset(rules=[
        PermissionRule(action="allow", permission="*"),
        PermissionRule(action="deny", permission="web_search"),
    ])
    _reasoning, _tokens, excluded, native = await processor._build_stream_args()
    assert native is False
    assert excluded == {"web_search"}


@pytest.mark.asyncio
async def test_goal_current_deny_blocks_tool_before_execution(monkeypatch) -> None:
    old_goal = Ruleset(rules=[
        PermissionRule(action="allow", permission="*"),
        PermissionRule(action="allow", permission="bash"),
    ])
    current_agent = Ruleset(rules=[
        PermissionRule(action="allow", permission="*"),
        PermissionRule(action="deny", permission="bash"),
    ])
    effective = _merge_prompt_permission_layers(
        current_agent,
        Ruleset(),
        old_goal,
        Ruleset(),
        request_is_authoritative=True,
        enforce_current_ceiling=True,
    )
    tool = SimpleNamespace(
        id="bash",
        requires_approval=False,
        execute=AsyncMock(),
    )
    job = GenerationJob(
        "goal-denied-tool-stream",
        "goal-denied-tool-session",
        invocation_source="goal",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        tool_registry=SimpleNamespace(
            get=lambda name: tool if name == "bash" else None,
        ),
        merged_permissions=effective,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    monkeypatch.setattr(processor_module, "_audit_tool_event", AsyncMock())
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "denied-bash-call",
        "name": "bash",
        "arguments": {"command": "touch must-not-run"},
    }))

    assert tool.execute.await_count == 0
    assert processor._streaming_executor.has_submissions is False


@pytest.mark.asyncio
async def test_goal_current_search_deny_disables_provider_native_search(
    monkeypatch,
) -> None:
    old_goal = Ruleset(rules=[
        PermissionRule(action="allow", permission="*"),
        PermissionRule(action="allow", permission="web_search"),
    ])
    current_agent = Ruleset(rules=[
        PermissionRule(action="allow", permission="*"),
        PermissionRule(action="deny", permission="web_search"),
    ])
    effective = _merge_prompt_permission_layers(
        current_agent,
        Ruleset(),
        old_goal,
        Ruleset(),
        request_is_authoritative=True,
        enforce_current_ceiling=True,
    )
    prompt = SimpleNamespace(
        job=GenerationJob(
            "goal-native-search-stream",
            "goal-native-search-session",
            invocation_source="goal",
        ),
        request=SimpleNamespace(reasoning=None),
        model_info=None,
        tool_registry=SimpleNamespace(is_enabled=lambda _tool_id: True),
        provider=SimpleNamespace(id="openai-subscription"),
        merged_permissions=effective,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    monkeypatch.setattr(
        processor_module,
        "_search_quota",
        SimpleNamespace(get_quota=AsyncMock(return_value=(0, False))),
    )

    _reasoning, _tokens, excluded, native = await processor._build_stream_args()

    assert native is False
    assert excluded == {"web_search"}


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


def test_image_generation_permission_exposes_per_call_cost_risk() -> None:
    message = _permission_message(
        "image_generate",
        {"prompt": "cat", "image_size": "1024x1024"},
        truncated=False,
        language="en",
    )
    metadata = _permission_metadata(
        "image_generate",
        {"prompt": "cat", "image_size": "1024x1024"},
    )

    assert "external provider" in message
    assert "provider bill" in message
    assert "one generation only" in message
    assert metadata == {
        "provider": "siliconflow",
        "provider_name": "SiliconFlow",
        "model": "Kwai-Kolors/Kolors",
        "image_size": "1024x1024",
        "estimated_cost": 0.0,
        "currency": "CNY",
        "pricing_unit": "image",
        "pricing_basis": "official_catalog",
        "pricing_as_of": "2026-07-14",
        "pricing_source_url": "https://siliconflow.cn/pricing",
        "approval_mode": "per_call",
        "external_billing": True,
    }


def test_image_generation_permission_message_is_localized() -> None:
    message = _permission_message(
        "image_generate",
        {"prompt": "cat"},
        truncated=False,
        language="zh",
    )

    assert "外部供应商" in message
    assert "最终费用" in message
    assert "仅授权一次" in message


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
        def __init__(self, tool_id: str, *, requires_approval: bool = False) -> None:
            self.id = tool_id
            self.requires_approval = requires_approval

    class _Registry:
        def get(self, name: str):
            return {
                "bash": _Tool("bash"),
                "read": _Tool("read"),
                "tencent-docs_delete_space_node": _Tool(
                    "tencent-docs_delete_space_node",
                    requires_approval=True,
                ),
            }.get(name)

    job = GenerationJob(
        "headless-stream",
        "headless-session",
        invocation_source="desktop",
    )
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

    # A tool-owned approval floor must override the broad global allow. This
    # is how destructive Tencent Docs tools fail closed in automations.
    destructive_job = GenerationJob(
        "destructive-stream",
        "destructive-session",
        invocation_source="desktop",
    )
    destructive_job.interactive = False
    destructive_prompt = SimpleNamespace(
        job=destructive_job,
        session_factory=object(),
        tool_registry=_Registry(),
        merged_permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
    )
    destructive_processor = SessionProcessor(
        destructive_prompt,
        [],
        "destructive-assistant-message",
    )
    destructive_processor._init_step_state()

    await destructive_processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "destructive-call",
        "name": "tencent-docs_delete_space_node",
        "arguments": {"node_id": "node-1", "remove_type": "all"},
    }))

    assert destructive_processor._exec_blocked is True
    assert destructive_processor.finish_reason == "error"
    assert destructive_processor._streaming_executor.has_submissions is False
    assert any(event.event == AGENT_ERROR for event in destructive_job.events)


@pytest.mark.asyncio
async def test_invocation_source_hard_deny_precedes_broad_tool_allow(
    monkeypatch,
) -> None:
    tool = SimpleNamespace(id="write", requires_approval=False)
    registry = SimpleNamespace(get=lambda name: tool if name == "write" else None)
    job = GenerationJob(
        "scheduler-write-stream",
        "scheduler-write-session",
        invocation_source="scheduler",
        invocation_source_id="task-denied",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        tool_registry=registry,
        merged_permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "_audit_tool_event", audit)
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "write-call",
        "name": "write",
        "arguments": {"file_path": "blocked.txt", "content": "blocked"},
    }))

    assert processor._exec_blocked is True
    assert processor.finish_reason == "error"
    assert processor._streaming_executor.has_submissions is False
    assert audit.await_args.kwargs["job"] is job
    assert audit.await_args.kwargs["decision"] == "deny"
    assert audit.await_args.kwargs["outcome"] == "blocked"
    assert audit.await_args.kwargs["extra_details"] == {
        "blocked_capabilities": "filesystem_write",
    }
    assert any(
        event.event == AGENT_ERROR
        and event.data.get("error_type") == "invocation_source_denied"
        for event in job.events
    )


@pytest.mark.asyncio
async def test_unknown_tool_is_hard_denied_for_desktop_wildcard(
    monkeypatch,
) -> None:
    tool = SimpleNamespace(
        id="future_plugin_side_effect",
        requires_approval=False,
    )
    registry = SimpleNamespace(
        get=lambda name: tool if name == tool.id else None,
    )
    job = GenerationJob(
        "desktop-unknown-tool",
        "desktop-unknown-session",
        invocation_source="desktop",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        tool_registry=registry,
        merged_permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "_audit_tool_event", audit)
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "unknown-call",
        "name": tool.id,
        "arguments": {},
    }))

    assert processor._exec_blocked is True
    assert processor._streaming_executor.has_submissions is False
    assert audit.await_args.kwargs["extra_details"] == {
        "blocked_capabilities": "unknown_tool",
    }
    assert any(
        event.event == AGENT_ERROR
        and event.data.get("error_type") == "invocation_source_denied"
        for event in job.events
    )


@pytest.mark.asyncio
async def test_privileged_tool_is_not_submitted_when_pre_action_audit_fails(
    monkeypatch,
) -> None:
    tool = SimpleNamespace(id="write", requires_approval=False)
    registry = SimpleNamespace(get=lambda name: tool if name == "write" else None)
    job = GenerationJob(
        "desktop-write-stream",
        "desktop-write-session",
        invocation_source="desktop",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        tool_registry=registry,
        merged_permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    audit = AsyncMock(side_effect=AuditPersistenceError("database unavailable"))
    monkeypatch.setattr(processor_module, "_audit_tool_event", audit)
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "write-call",
        "name": "write",
        "arguments": {"file_path": "blocked.txt", "content": "blocked"},
    }))

    assert audit.await_args.kwargs["required"] is True
    assert processor._streaming_executor.has_submissions is False
    assert processor._exec_blocked is True
    assert any(
        event.event == AGENT_ERROR
        and event.data.get("error_type") == "security_audit_unavailable"
        for event in job.events
    )


@pytest.mark.asyncio
async def test_provider_call_does_not_start_when_required_audit_fails(
    monkeypatch,
) -> None:
    job = GenerationJob(
        "provider-audit-failure",
        "provider-audit-session",
        invocation_source="desktop",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        provider=SimpleNamespace(id="test-provider"),
        model_id="test-model",
        system_prompt="system",
        agent=SimpleNamespace(),
        tool_registry=SimpleNamespace(),
        discovered_tools=set(),
        request=SimpleNamespace(format=None),
        step=1,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    processor._build_stream_args = AsyncMock(return_value=(None, 128, None, False))
    processor._check_vision_blocked = AsyncMock(return_value=None)
    stream_started = False

    async def fake_stream_llm(*_args, **_kwargs):
        nonlocal stream_started
        stream_started = True
        yield SimpleNamespace(type="finish", data={"reason": "stop"})

    async def fail_required_audit(*_args, **kwargs):
        assert kwargs["required"] is True
        raise AuditPersistenceError("database unavailable")

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)
    monkeypatch.setattr(
        processor_module,
        "record_security_event",
        fail_required_audit,
    )

    assert await processor._stream_llm_with_retry() is None
    assert stream_started is False
    assert isinstance(processor._stream_error, AuditPersistenceError)


@pytest.mark.asyncio
async def test_unknown_root_source_cannot_start_provider_inference(
    monkeypatch,
) -> None:
    job = GenerationJob("unknown-provider-stream", "unknown-provider-session")
    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        provider=SimpleNamespace(id="test-provider"),
        model_id="test-model",
        system_prompt="system",
        agent=SimpleNamespace(),
        tool_registry=SimpleNamespace(),
        discovered_tools=set(),
        request=SimpleNamespace(format=None),
        step=1,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    audit = AsyncMock()
    stream_llm = AsyncMock()
    monkeypatch.setattr(processor_module, "record_security_event", audit)
    monkeypatch.setattr(processor_module, "stream_llm", stream_llm)

    assert await processor._stream_llm_with_retry() is None
    stream_llm.assert_not_awaited()
    assert audit.await_args.kwargs["decision"] == "deny"
    assert audit.await_args.kwargs["outcome"] == "blocked"
    assert isinstance(processor._stream_error, PermissionError)
