"""Strict execution-order contracts for SessionProcessor middleware wiring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.permission import Ruleset
from app.schemas.agent import PermissionRule
from app.session import processor as processor_module
from app.session.middleware import (
    Middleware,
    MiddlewareChain,
    MiddlewareContext,
    ToolAction,
)
from app.session.processor import SessionProcessor
from app.streaming.events import AGENT_ERROR, TEXT_DELTA, TOOL_START
from app.streaming.manager import GenerationJob


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Database:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return _Transaction()


def _stream_processor(
    chain: MiddlewareChain,
    *,
    stream_id: str,
) -> tuple[SessionProcessor, GenerationJob]:
    job = GenerationJob(
        stream_id,
        f"{stream_id}-session",
        invocation_source="desktop",
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=lambda: _Database(),
        provider=SimpleNamespace(id="test-provider"),
        model_id="test-model",
        system_prompt="system",
        agent=SimpleNamespace(name="build"),
        tool_registry=SimpleNamespace(),
        discovered_tools=set(),
        request=SimpleNamespace(format=None),
        step=1,
        middleware_chain=chain,
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
    processor._build_stream_args = AsyncMock(
        return_value=(None, 128, None, False)
    )
    processor._check_vision_blocked = AsyncMock(return_value=None)
    return processor, job


class _RewriteText(Middleware):
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    async def after_llm_response(self, text, tool_calls, ctx):
        self.calls.append((text, tool_calls))
        return "rewritten response", tool_calls


@pytest.mark.asyncio
async def test_after_llm_text_transform_is_the_only_visible_and_persisted_text(
    monkeypatch,
) -> None:
    middleware = _RewriteText()
    processor, job = _stream_processor(
        MiddlewareChain([middleware]),
        stream_id="after-llm-text",
    )
    audit = AsyncMock()
    create_part = AsyncMock()
    monkeypatch.setattr(processor_module, "record_security_event", audit)
    monkeypatch.setattr(processor_module, "create_part", create_part)

    async def fake_stream_llm(*_args, **_kwargs):
        yield SimpleNamespace(type="text-delta", data={"text": "original "})
        yield SimpleNamespace(type="text-delta", data={"text": "response"})
        yield SimpleNamespace(type="finish", data={"reason": "stop"})

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)

    assert await processor._stream_llm_with_retry() is None
    assert middleware.calls == [("original response", [])]
    assert processor._accumulated_text == "rewritten response"
    assert [
        event.data["text"]
        for event in job.events
        if event.event == TEXT_DELTA
    ] == ["rewritten response"]

    await processor._persist_text_and_reasoning()
    create_part.assert_awaited_once()
    assert create_part.await_args.kwargs["data"] == {
        "type": "text",
        "text": "rewritten response",
    }


class _ReplaceToolCalls(Middleware):
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def after_llm_response(self, text, tool_calls, ctx):
        self.order.append("after-llm")
        assert [call["id"] for call in tool_calls] == ["original-call"]
        return text, [{
            "id": "replacement-call",
            "name": "read",
            "arguments": {"file_path": "safe.txt"},
        }]


@pytest.mark.asyncio
async def test_after_llm_tool_transform_defers_dispatch_until_stream_finishes(
    monkeypatch,
) -> None:
    order: list[str] = []
    processor, _job = _stream_processor(
        MiddlewareChain([_ReplaceToolCalls(order)]),
        stream_id="after-llm-tool",
    )
    handle_tool = AsyncMock(side_effect=lambda _chunk: order.append("dispatch"))
    processor._handle_tool_call_chunk = handle_tool
    monkeypatch.setattr(processor_module, "record_security_event", AsyncMock())

    async def fake_stream_llm(*_args, **_kwargs):
        yield SimpleNamespace(type="tool-call", data={
            "id": "original-call",
            "name": "write",
            "arguments": {"file_path": "unsafe.txt", "content": "x"},
        })
        order.append("provider-stream-finished")
        yield SimpleNamespace(type="finish", data={"reason": "tool_calls"})

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)

    assert await processor._stream_llm_with_retry() is None
    assert order == ["provider-stream-finished", "after-llm", "dispatch"]
    handle_tool.assert_awaited_once()
    assert handle_tool.await_args.args[0].data == {
        "id": "replacement-call",
        "name": "read",
        "arguments": {"file_path": "safe.txt"},
    }
    assert processor._tool_calls_in_step == [handle_tool.await_args.args[0].data]


@pytest.mark.asyncio
async def test_no_after_llm_override_preserves_immediate_streaming_tool_dispatch(
    monkeypatch,
) -> None:
    order: list[str] = []
    processor, _job = _stream_processor(
        MiddlewareChain([Middleware()]),
        stream_id="after-llm-noop",
    )
    processor._handle_tool_call_chunk = AsyncMock(
        side_effect=lambda _chunk: order.append("dispatch")
    )
    monkeypatch.setattr(processor_module, "record_security_event", AsyncMock())

    async def fake_stream_llm(*_args, **_kwargs):
        yield SimpleNamespace(type="tool-call", data={
            "id": "streamed-call",
            "name": "read",
            "arguments": {"file_path": "safe.txt"},
        })
        order.append("provider-stream-continued")
        yield SimpleNamespace(type="finish", data={"reason": "tool_calls"})

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)

    assert await processor._stream_llm_with_retry() is None
    assert order == ["dispatch", "provider-stream-continued"]


class _FailAfterLlm(Middleware):
    async def after_llm_response(self, text, tool_calls, ctx):
        raise RuntimeError("middleware failed")


@pytest.mark.asyncio
async def test_after_llm_failure_has_no_visible_text_or_tool_dispatch(
    monkeypatch,
) -> None:
    processor, job = _stream_processor(
        MiddlewareChain([_FailAfterLlm()]),
        stream_id="after-llm-failure",
    )
    processor._handle_tool_call_chunk = AsyncMock()
    monkeypatch.setattr(processor_module, "record_security_event", AsyncMock())
    create_part = AsyncMock()
    monkeypatch.setattr(processor_module, "create_part", create_part)
    monkeypatch.setattr(
        processor_module,
        "_delete_empty_assistant_messages",
        AsyncMock(),
    )

    async def fake_stream_llm(*_args, **_kwargs):
        yield SimpleNamespace(type="text-delta", data={"text": "must stay hidden"})
        yield SimpleNamespace(type="tool-call", data={
            "id": "must-not-run",
            "name": "write",
            "arguments": {"file_path": "unsafe.txt", "content": "x"},
        })
        yield SimpleNamespace(type="finish", data={"reason": "tool_calls"})

    monkeypatch.setattr(processor_module, "stream_llm", fake_stream_llm)

    assert await processor._stream_llm_with_retry() is None
    assert isinstance(processor._stream_error, RuntimeError)
    assert processor._accumulated_text == ""
    assert processor._tool_calls_in_step == []
    assert not any(event.event == TEXT_DELTA for event in job.events)
    processor._handle_tool_call_chunk.assert_not_awaited()

    assert await processor._handle_stream_error() == "stop"
    create_part.assert_not_awaited()


class _BlockTool(Middleware):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def before_tool_exec(self, tool_name, tool_args, ctx):
        self.calls.append((tool_name, tool_args))
        return ToolAction(
            action="block",
            message="blocked by test policy",
            code="test_policy_block",
        )


def _tool_processor(
    middleware: Middleware,
    *,
    permissions: Ruleset,
) -> tuple[SessionProcessor, GenerationJob, SimpleNamespace]:
    job = GenerationJob(
        "before-tool-stream",
        "before-tool-session",
        invocation_source="desktop",
    )
    tool = SimpleNamespace(
        id="write",
        requires_approval=False,
        is_concurrency_safe=False,
        execute=AsyncMock(),
    )
    chain = MiddlewareChain([middleware])
    prompt = SimpleNamespace(
        job=job,
        session_factory=lambda: _Database(),
        tool_registry=SimpleNamespace(
            get=lambda name: tool if name == "write" else None,
        ),
        merged_permissions=permissions,
        middleware_chain=chain,
        request=SimpleNamespace(
            language="en",
            _goal_permission_baseline=None,
        ),
        agent=SimpleNamespace(name="build"),
        workspace=None,
        discovered_tools=set(),
        attachment_paths=frozenset(),
        provider_registry=SimpleNamespace(),
        agent_registry=SimpleNamespace(),
        model_id="test-model",
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
    return processor, job, tool


@pytest.mark.asyncio
async def test_before_tool_block_occurs_before_part_audit_executor_and_side_effect(
    monkeypatch,
) -> None:
    middleware = _BlockTool()
    processor, job, tool = _tool_processor(
        middleware,
        permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
    )
    persist_error = AsyncMock()
    create_part = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "_persist_tool_error", persist_error)
    monkeypatch.setattr(processor_module, "create_part", create_part)
    monkeypatch.setattr(processor_module, "_audit_tool_event", audit)

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "blocked-call",
        "name": "write",
        "arguments": {"file_path": "blocked.txt", "content": "no"},
    }))

    assert middleware.calls == [(
        "write",
        {"file_path": "blocked.txt", "content": "no"},
    )]
    assert processor._exec_blocked is True
    assert processor._streaming_executor.has_submissions is False
    tool.execute.assert_not_awaited()
    create_part.assert_not_awaited()
    audit.assert_not_awaited()
    persist_error.assert_awaited_once()
    assert not any(event.event == TOOL_START for event in job.events)
    assert any(
        event.event == AGENT_ERROR
        and event.data.get("error_type") == "test_policy_block"
        for event in job.events
    )


class _AllowTool(Middleware):
    def __init__(self) -> None:
        self.called = False

    async def before_tool_exec(self, tool_name, tool_args, ctx):
        self.called = True
        return ToolAction(action="allow")


@pytest.mark.asyncio
async def test_before_tool_allow_cannot_override_permission_deny(
    monkeypatch,
) -> None:
    middleware = _AllowTool()
    processor, _job, tool = _tool_processor(
        middleware,
        permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="deny", permission="write"),
        ]),
    )
    create_part = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())
    monkeypatch.setattr(processor_module, "create_part", create_part)
    monkeypatch.setattr(processor_module, "_audit_tool_event", audit)

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "permission-denied-call",
        "name": "write",
        "arguments": {"file_path": "blocked.txt", "content": "no"},
    }))

    assert middleware.called is True
    assert processor._streaming_executor.has_submissions is False
    tool.execute.assert_not_awaited()
    create_part.assert_not_awaited()
    assert audit.await_args.kwargs["decision"] == "deny"
    assert audit.await_args.kwargs["outcome"] == "denied"


class _MutateToolArgs(Middleware):
    async def before_tool_exec(self, tool_name, tool_args, ctx):
        tool_args["file_path"] = "allowed.txt"
        return ToolAction(action="allow")


@pytest.mark.asyncio
async def test_before_tool_cannot_mutate_arguments_to_bypass_permission(
    monkeypatch,
) -> None:
    processor, _job, tool = _tool_processor(
        _MutateToolArgs(),
        permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(
                action="deny",
                permission="write",
                pattern="protected.txt",
            ),
        ]),
    )
    persist_error = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(processor_module, "_persist_tool_error", persist_error)
    monkeypatch.setattr(processor_module, "_audit_tool_event", audit)

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "mutation-attempt",
        "name": "write",
        "arguments": {"file_path": "protected.txt", "content": "no"},
    }))

    assert processor._streaming_executor.has_submissions is False
    tool.execute.assert_not_awaited()
    persist_error.assert_awaited_once()
    assert persist_error.await_args.args[5] == {
        "file_path": "protected.txt",
        "content": "no",
    }
    assert audit.await_args.kwargs["decision"] == "deny"


class _InvalidToolAction(Middleware):
    async def before_tool_exec(self, tool_name, tool_args, ctx):
        return ToolAction(action="elevate")


@pytest.mark.asyncio
async def test_invalid_before_tool_action_fails_closed(monkeypatch) -> None:
    processor, job, tool = _tool_processor(
        _InvalidToolAction(),
        permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
    )
    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "invalid-middleware-call",
        "name": "write",
        "arguments": {"file_path": "blocked.txt", "content": "no"},
    }))

    assert processor._streaming_executor.has_submissions is False
    tool.execute.assert_not_awaited()
    assert any(
        event.event == AGENT_ERROR
        and event.data.get("error_type") == "middleware_contract_error"
        for event in job.events
    )
