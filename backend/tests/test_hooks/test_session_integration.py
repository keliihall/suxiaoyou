from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import release_features
from app.agent.agent import AgentRegistry
from app.hooks.dispatcher import HookDispatcher
from app.hooks.models import BuiltinHookDeclaration, HookCommandDeclaration
from app.hooks.registry import HookRegistry
from app.hooks.runtime import HookRuntime
from app.hooks.trust import HookTrustStore
from app.schemas.agent import PermissionRule, Ruleset
from app.schemas.chat import PromptRequest
from app.schemas.provider import ModelInfo
from app.session import processor as processor_module
from app.session.middleware import MiddlewareContext
from app.session.processor import SessionProcessor
from app.session.prompt import SessionPrompt
from app.streaming.events import PERMISSION_REQUEST, TOOL_START
from app.streaming.manager import GenerationJob
from app.tool.base import ToolResult
from app.tool.registry import ToolRegistry


class _Provider:
    id = "hook-test-provider"


class _ProviderRegistry:
    def __init__(self, *, resolved: bool = True) -> None:
        self.provider = _Provider()
        self.model = ModelInfo(
            id="hook-test-model",
            name="Hook Test Model",
            provider_id=self.provider.id,
        )
        self.resolve_model = MagicMock(
            return_value=(self.provider, self.model) if resolved else None
        )
        self.refresh_models = AsyncMock(return_value={})
        self.all_models = MagicMock(return_value=[self.model])


class _Database:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def get(self, *_args):
        return None


def _prompt(
    session_factory,
    workspace: Path,
    *,
    session_id: str = "hook-session",
    depth: int = 0,
    provider_registry: _ProviderRegistry | None = None,
) -> SessionPrompt:
    job = GenerationJob(
        f"{session_id}-stream",
        session_id,
        invocation_source="desktop",
    )
    job._depth = depth
    return SessionPrompt(
        job,
        PromptRequest(
            session_id=session_id,
            text="review the workspace",
            model="hook-test-model",
            workspace=str(workspace),
        ),
        session_factory=session_factory,
        provider_registry=provider_registry or _ProviderRegistry(),
        agent_registry=AgentRegistry(),
        tool_registry=ToolRegistry(),
    )


def _attach_runtime(
    prompt: SessionPrompt,
    root: Path,
    *,
    handler=None,
    event: str = "PreToolUse",
) -> HookRegistry:
    registry = HookRegistry(root)
    if handler is not None:
        registry.register_builtin(
            BuiltinHookDeclaration(
                hook_id=f"integration-{event}",
                event=event,
                failure_policy="required",
            ),
            handler,
        )
    trust = HookTrustStore(root, storage_root=root / ".hook-trust")
    prompt.hook_runtime = HookRuntime(
        prompt.job,
        HookDispatcher(registry, trust, enabled=True),
    )
    prompt._hook_workspace = str(root)
    prompt.checkpoint_binding = None
    return registry


def _processor_prompt(
    root: Path,
    *,
    job: GenerationJob,
    permissions: Ruleset,
    tool,
) -> SessionPrompt:
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.job = job
    prompt.session_factory = lambda: _Database()
    prompt.tool_registry = SimpleNamespace(
        get=lambda name: tool if name == tool.id else None,
    )
    prompt.merged_permissions = permissions
    prompt.middleware_chain = SimpleNamespace(
        run_before_tool_exec=AsyncMock(
            return_value=SimpleNamespace(action="allow", message=None, code=None)
        )
    )
    prompt.request = SimpleNamespace(
        language="en",
        _goal_permission_baseline=None,
    )
    prompt.agent = SimpleNamespace(name="build", tools=[])
    prompt.workspace = str(root)
    prompt.discovered_tools = set()
    prompt.attachment_paths = frozenset()
    prompt.provider_registry = SimpleNamespace()
    prompt.agent_registry = SimpleNamespace()
    prompt.model_id = "hook-test-model"
    prompt.checkpoint_binding = None
    prompt._record_tool_checkpoint_effects = AsyncMock(return_value=0)
    return prompt


async def _wait_for_response_count(job: GenerationJob, count: int) -> None:
    for _index in range(200):
        if len(job._response_requests) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Expected {count} response requests")


@pytest.mark.asyncio
async def test_invalid_project_config_fails_before_provider_resolution(
    tmp_path: Path,
    session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    config = tmp_path / ".suxiaoyou" / "hooks.json"
    config.parent.mkdir()
    config.write_text(
        '{"version":1,"hooks":[],"unexpected":true}',
        encoding="utf-8",
    )
    providers = _ProviderRegistry()
    prompt = _prompt(
        session_factory,
        tmp_path,
        provider_registry=providers,
    )

    with pytest.raises(RuntimeError, match="configuration failed closed"):
        await prompt._setup()

    providers.all_models.assert_not_called()
    providers.resolve_model.assert_not_called()
    providers.refresh_models.assert_not_awaited()
    assert prompt.job.events == []


@pytest.mark.asyncio
async def test_closed_gate_ignores_project_config_and_preserves_provider_path(
    tmp_path: Path,
    session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", False)
    config = tmp_path / ".suxiaoyou" / "hooks.json"
    config.parent.mkdir()
    config.write_text("not-json", encoding="utf-8")
    providers = _ProviderRegistry(resolved=False)
    prompt = _prompt(
        session_factory,
        tmp_path,
        provider_registry=providers,
    )

    with pytest.raises(RuntimeError, match="Model not found"):
        await prompt._setup()

    providers.resolve_model.assert_called()
    providers.refresh_models.assert_awaited_once()
    assert not any(event.event_type.startswith("hook.") for event in prompt.job.lifecycle_events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        (0, ["session.started", "user_prompt.submitted", "turn.stopped"]),
        (
            1,
            [
                "session.started",
                "subagent.started",
                "user_prompt.submitted",
                "turn.stopped",
                "subagent.stopped",
            ],
        ),
    ],
)
async def test_session_and_subagent_boundaries_emit_once(
    tmp_path: Path,
    session_factory,
    monkeypatch,
    depth: int,
    expected: list[str],
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    # Isolate Hook boundary events from the separately tested checkpoint and
    # post-checkpoint validation lifecycle.
    monkeypatch.setattr(release_features, "V11_CHECKPOINTS_RELEASED", False)
    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", False)
    monkeypatch.setattr(release_features, "V11_VALIDATION_AGENT_RELEASED", False)
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))
    workspace = tmp_path / f"workspace-{depth}"
    workspace.mkdir()
    prompt = _prompt(
        session_factory,
        workspace,
        session_id=f"boundary-session-{depth}",
        depth=depth,
    )
    prompt._loop = AsyncMock()
    prompt._post_loop = AsyncMock()

    await prompt.run()

    semantic = [
        event.event_type
        for event in prompt.job.lifecycle_events
        if event.event_type != "hook.dispatch.completed"
    ]
    assert semantic == expected
    assert all(semantic.count(event_type) == 1 for event_type in expected)
    assert len([
        event for event in prompt.job.lifecycle_events
        if event.event_type == "hook.dispatch.completed"
    ]) == len(expected)
    prompt._loop.assert_awaited_once()
    prompt._post_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_tool_deny_precedes_middleware_audit_part_and_executor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    job = GenerationJob("deny-stream", "deny-session", invocation_source="desktop")
    tool = SimpleNamespace(
        id="write",
        requires_approval=False,
        is_concurrency_safe=False,
        execute=AsyncMock(return_value=ToolResult(output="must not run")),
    )
    prompt = _processor_prompt(
        tmp_path,
        job=job,
        permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
        tool=tool,
    )
    _attach_runtime(
        prompt,
        tmp_path,
        handler=lambda _event: {"version": 1, "decision": "deny"},
    )
    processor = SessionProcessor(
        prompt,
        [],
        "assistant-message",
        middleware_ctx=MiddlewareContext(
            session_id=job.session_id,
            step=1,
            job=job,
        ),
    )
    processor._init_step_state()
    persist_error = AsyncMock()
    create_part = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "_persist_tool_error", persist_error)
    monkeypatch.setattr(processor_module, "create_part", create_part)
    monkeypatch.setattr(processor_module, "_audit_tool_event", audit)

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "deny-call",
        "name": "write",
        "arguments": {"file_path": "blocked.txt", "content": "no"},
    }))

    assert processor._streaming_executor.has_submissions is False
    tool.execute.assert_not_awaited()
    prompt.middleware_chain.run_before_tool_exec.assert_not_awaited()
    create_part.assert_not_awaited()
    audit.assert_not_awaited()
    prompt._record_tool_checkpoint_effects.assert_not_awaited()
    persist_error.assert_awaited_once()
    assert not any(event.event == TOOL_START for event in job.events)
    assert [
        event.event_type for event in job.lifecycle_events
        if event.event_type in {"tool.pre_use", "hook.dispatch.completed"}
    ] == ["tool.pre_use", "hook.dispatch.completed"]


@pytest.mark.asyncio
async def test_tool_permission_and_exact_hook_command_approval_are_distinct(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    marker = tmp_path / "hook-command-ran"
    executable = tmp_path / "pre-policy"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "event = json.load(sys.stdin)\n"
        "assert event['payload']['permission_decision'] == 'allow'\n"
        f"pathlib.Path({str(marker)!r}).write_text(event['event_id'])\n"
        "print(json.dumps({'version': 1, 'decision': 'allow'}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    job = GenerationJob("ask-stream", "ask-session", invocation_source="desktop")
    job.interactive = True
    tool = SimpleNamespace(
        id="read",
        requires_approval=False,
        is_concurrency_safe=True,
        execute=AsyncMock(return_value=ToolResult(output="ok")),
    )
    prompt = _processor_prompt(
        tmp_path,
        job=job,
        permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="read"),
        ]),
        tool=tool,
    )
    registry = _attach_runtime(prompt, tmp_path)
    registry.register_project_commands([
        HookCommandDeclaration(
            hook_id="exact-pre-policy",
            event="PreToolUse",
            failure_policy="required",
            command=(executable.name,),
        )
    ])
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())
    monkeypatch.setattr(processor_module, "create_part", AsyncMock())
    monkeypatch.setattr(processor_module, "_audit_tool_event", AsyncMock())

    task = asyncio.create_task(processor._handle_tool_call_chunk(
        SimpleNamespace(data={
            "id": "ask-call",
            "name": "read",
            "arguments": {"file_path": "report.txt"},
        })
    ))
    await _wait_for_response_count(job, 1)
    ordinary = list(job._response_requests.values())[0]
    assert ordinary.tool == "read"
    job.resolve_response(
        ordinary.call_id,
        {"allowed": True, "remember": False},
        source="test",
    )

    await _wait_for_response_count(job, 2)
    hook_approval = list(job._response_requests.values())[1]
    assert hook_approval.call_id != ordinary.call_id
    assert hook_approval.tool == "hook_command"
    assert not marker.exists()
    job.resolve_response(
        hook_approval.call_id,
        {"allowed": True},
        source="test",
    )
    await task

    assert marker.exists()
    hook_request = next(
        event for event in job.events
        if event.event == PERMISSION_REQUEST
        and event.data.get("permission") == "hook_command"
    )
    descriptor = hook_request.data["arguments"]
    assert descriptor["fingerprint"].startswith("sha256:")
    assert hook_request.data["metadata"]["fingerprint"] == descriptor["fingerprint"]
    assert processor._streaming_executor.has_submissions is True
    assert any(event.event == TOOL_START for event in job.events)
    await processor._streaming_executor.collect()


@pytest.mark.asyncio
async def test_compaction_boundaries_are_paired_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.job = GenerationJob("compact-stream", "compact-session")
    prompt.step = 3
    prompt.assistant_msg_id = "assistant-message"
    prompt.checkpoint_binding = None
    prompt.workspace = None
    prompt.current_todos = []
    prompt._context_collapse_exhausted = True
    prompt._consecutive_compact_failures = 0
    prompt.session_factory = object()
    prompt.provider_registry = object()
    prompt.agent_registry = object()
    prompt.model_id = "hook-test-model"
    _attach_runtime(prompt, tmp_path)
    compact = AsyncMock()
    monkeypatch.setattr("app.session.compaction.run_compaction", compact)

    assert await prompt._handle_compact_result() is False

    compact.assert_awaited_once()
    semantic = [
        event.event_type for event in prompt.job.lifecycle_events
        if event.event_type != "hook.dispatch.completed"
    ]
    assert semantic == ["compaction.pre", "compaction.post"]


@pytest.mark.asyncio
async def test_post_tool_projection_never_contains_raw_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    observed = []
    job = GenerationJob("post-stream", "post-session")
    tool = SimpleNamespace(id="read")
    prompt = _processor_prompt(
        tmp_path,
        job=job,
        permissions=Ruleset(),
        tool=tool,
    )
    _attach_runtime(
        prompt,
        tmp_path,
        event="PostToolUse",
        handler=lambda event: (
            observed.append(event),
            {"version": 1, "decision": "continue"},
        )[1],
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    secret = "raw-tool-output-must-not-enter-hook-lifecycle"

    await processor._dispatch_post_tool_hook(
        tool=tool,
        tool_args={"file_path": "report.txt", "api_key": "hidden"},
        call_id="post-call",
        outcome="success",
        result=ToolResult(
            output=secret,
            metadata={"format": "text"},
        ),
    )

    assert observed[0].payload["output_length"] == len(secret)
    assert "output" not in observed[0].payload
    serialized = json.dumps(
        [event.to_dict() for event in job.lifecycle_events],
        ensure_ascii=False,
    )
    assert secret not in serialized
    assert "stdout" not in serialized
    assert "logs" not in serialized


@pytest.mark.asyncio
async def test_dynamic_gate_closure_restores_tool_admission_mid_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    job = GenerationJob("gate-stream", "gate-session", invocation_source="desktop")
    tool = SimpleNamespace(
        id="read",
        requires_approval=False,
        is_concurrency_safe=True,
        execute=AsyncMock(return_value=ToolResult(output="ok")),
    )
    prompt = _processor_prompt(
        tmp_path,
        job=job,
        permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
        tool=tool,
    )
    _attach_runtime(prompt, tmp_path)

    async def close_gate(*_args, **_kwargs):
        monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", False)
        return None

    prompt.dispatch_hook_event = AsyncMock(side_effect=close_gate)
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    monkeypatch.setattr(processor_module, "create_part", AsyncMock())
    monkeypatch.setattr(processor_module, "_audit_tool_event", AsyncMock())

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "gate-call",
        "name": "read",
        "arguments": {"file_path": "report.txt"},
    }))

    prompt.dispatch_hook_event.assert_awaited_once()
    assert processor._streaming_executor.has_submissions is True
    assert any(event.event == TOOL_START for event in job.events)
    await processor._streaming_executor.collect()


@pytest.mark.asyncio
async def test_post_tool_boundary_is_not_duplicated_by_finalization_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    observed = []
    job = GenerationJob("once-stream", "once-session")
    tool = SimpleNamespace(id="read")
    prompt = _processor_prompt(
        tmp_path,
        job=job,
        permissions=Ruleset(),
        tool=tool,
    )
    _attach_runtime(
        prompt,
        tmp_path,
        event="PostToolUse",
        handler=lambda event: (
            observed.append(event),
            {"version": 1, "decision": "continue"},
        )[1],
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    meta = {
        "tool_part_id": "part-once",
        "loop_result": SimpleNamespace(action="allow", message=None),
        "tool": tool,
        "tool_args": {"file_path": "report.txt"},
        "call_id": "call-once",
        "permission_decision": "allow",
    }
    processor._exec_metadata = {0: meta}
    processor._has_tool_calls = True
    processor._streaming_executor = SimpleNamespace(
        has_submissions=True,
        collect=AsyncMock(return_value=[SimpleNamespace(
            index=0,
            result=ToolResult(output="ok"),
        )]),
    )

    async def fail_after_post_hook(meta_arg, _exec_result):
        await processor._dispatch_post_tool_hook_once(
            meta_arg,
            outcome="error",
        )
        raise RuntimeError("finalization failed")

    monkeypatch.setattr(processor, "_finalize_one_tool_result", fail_after_post_hook)

    with pytest.raises(RuntimeError, match="finalization failed"):
        await processor._dispatch_tool_calls()

    assert len(observed) == 1
    assert [
        event.event_type for event in job.lifecycle_events
        if event.event_type == "tool.post_use"
    ] == ["tool.post_use"]
