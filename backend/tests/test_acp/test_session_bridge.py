from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from acp.schema import (
    LoadSessionRequest,
    NewSessionRequest,
    PromptRequest,
    RequestPermissionResponse,
    SessionNotification,
)
import pytest
from sqlalchemy import select

from app.acp import BridgeRpcError, ProductionSessionPromptBridge
from app.errors import Conflict
from app.acp.session_bridge import (
    ACP_IDEMPOTENCY_REJECTED,
    ACP_INVALID_PARAMS,
    ACP_RUNTIME_LOCKED,
    ACP_SERVER_BUSY,
)
from app.models.idempotency_record import IdempotencyRecord
from app.runtime.events import LifecycleEventV1
from app.session.manager import (
    create_message,
    create_part,
    delete_session_cascade,
    get_session,
)
from app.streaming.events import (
    AGENT_ERROR,
    DONE,
    PERMISSION_REQUEST,
    PLAN_REVIEW,
    QUESTION,
    REASONING_DELTA,
    TEXT_DELTA,
    TOOL_RESULT,
    TOOL_START,
    SSEEvent,
)
from app.streaming.manager import GenerationJob, SessionBusyError, StreamManager

from .conftest import WireHarness


def _new_request(cwd: Path) -> NewSessionRequest:
    return NewSessionRequest.model_validate(
        {"cwd": str(cwd), "mcpServers": []}
    )


def _load_request(session_id: str, cwd: Path) -> LoadSessionRequest:
    return LoadSessionRequest.model_validate(
        {"sessionId": session_id, "cwd": str(cwd), "mcpServers": []}
    )


def _prompt_request(
    session_id: str,
    text: str = "hello",
    *,
    message_id: str | None = None,
) -> PromptRequest:
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": text}],
    }
    if message_id is not None:
        payload["messageId"] = message_id
    return PromptRequest.model_validate(payload)


def _bridge(
    session_factory,
    stream_manager: StreamManager,
    *,
    prompt_factory,
    cancellation_timeout_seconds: float = 1.0,
) -> ProductionSessionPromptBridge:
    return ProductionSessionPromptBridge(
        session_factory=session_factory,
        stream_manager=stream_manager,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
        index_manager=object(),
        prompt_factory=prompt_factory,
        cancellation_timeout_seconds=cancellation_timeout_seconds,
    )


class _NoopPrompt:
    def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
        self.job = job
        self.request = request
        self.kwargs = kwargs
        self.session_factory = kwargs["session_factory"]
        self.external_user_message_id = kwargs.get("external_user_message_id")
        self.recorded_external_user_message_id = None

    async def run(self) -> None:
        if self.external_user_message_id is not None:
            async with self.session_factory() as db:
                async with db.begin():
                    message = await create_message(
                        db,
                        session_id=self.job.session_id,
                        data={
                            "role": "user",
                            "acp_message_id": self.external_user_message_id,
                        },
                    )
                    await create_part(
                        db,
                        message_id=message.id,
                        session_id=self.job.session_id,
                        data={"type": "text", "text": self.request.text},
                    )
            self.recorded_external_user_message_id = self.external_user_message_id
        self.job.publish(
            SSEEvent(DONE, {"finish_reason": "stop", "session_id": self.job.session_id})
        )


@pytest.mark.asyncio
async def test_emergency_admission_guard_blocks_session_and_prompt_before_runtime(
    session_factory,
    tmp_path: Path,
) -> None:
    allowed = True
    bridge = ProductionSessionPromptBridge(
        session_factory=session_factory,
        stream_manager=StreamManager(),
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
        prompt_factory=_NoopPrompt,
        admission_guard=lambda: allowed,
    )
    created = await bridge.new_session(_new_request(tmp_path))
    allowed = False

    async def emit(_update) -> None:
        return None

    with pytest.raises(BridgeRpcError) as locked_prompt:
        await bridge.prompt(_prompt_request(created.session_id), emit)
    assert locked_prompt.value.code == ACP_RUNTIME_LOCKED
    assert locked_prompt.value.data == {"reason": "security_emergency_stop"}

    with pytest.raises(BridgeRpcError) as locked_session:
        await bridge.new_session(_new_request(tmp_path))
    assert locked_session.value.code == ACP_RUNTIME_LOCKED


@pytest.mark.asyncio
async def test_new_and_load_use_persistent_session_service_and_replay_only_visible_text(
    session_factory,
    tmp_path: Path,
) -> None:
    stream_manager = StreamManager()
    creator = _bridge(
        session_factory,
        stream_manager,
        prompt_factory=_NoopPrompt,
    )
    created = await creator.new_session(_new_request(tmp_path))

    async with session_factory() as db:
        async with db.begin():
            user = await create_message(
                db,
                session_id=created.session_id,
                data={
                    "role": "user",
                    "acp_message_id": "83206b18-8008-4f56-9517-969fee178b06",
                },
            )
            await create_part(
                db,
                message_id=user.id,
                session_id=created.session_id,
                data={"type": "text", "text": "visible user text"},
            )
            await create_part(
                db,
                message_id=user.id,
                session_id=created.session_id,
                data={"type": "file", "path": "/private/secret-file"},
            )
            assistant = await create_message(
                db,
                session_id=created.session_id,
                data={"role": "assistant", "error": "secret-error"},
            )
            await create_part(
                db,
                message_id=assistant.id,
                session_id=created.session_id,
                data={"type": "reasoning", "text": "secret reasoning"},
            )
            await create_part(
                db,
                message_id=assistant.id,
                session_id=created.session_id,
                data={
                    "type": "tool",
                    "state": {"input": {"api_key": "secret-key"}},
                },
            )
            await create_part(
                db,
                message_id=assistant.id,
                session_id=created.session_id,
                data={"type": "text", "text": "hidden summary", "synthetic": True},
            )
            await create_part(
                db,
                message_id=assistant.id,
                session_id=created.session_id,
                data={"type": "text", "text": "visible answer"},
            )

    loader = _bridge(
        session_factory,
        stream_manager,
        prompt_factory=_NoopPrompt,
    )
    updates: list[dict[str, Any]] = []

    async def emit(update) -> None:
        payload = dict(update)
        # Validate every production projection against the official union.
        SessionNotification.model_validate(
            {"sessionId": created.session_id, "update": payload}
        )
        updates.append(payload)

    response = await loader.load_session(
        _load_request(created.session_id, tmp_path), emit
    )

    assert response.model_dump(by_alias=True, exclude_none=True) == {}
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert [item["content"]["text"] for item in updates] == [
        "visible user text",
        "visible answer",
    ]
    assert updates[0]["messageId"] == "83206b18-8008-4f56-9517-969fee178b06"
    serialized = json.dumps(updates)
    assert "secret" not in serialized
    assert "/private" not in serialized

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(BridgeRpcError) as mismatch:
        await loader.load_session(_load_request(created.session_id, other), emit)
    assert mismatch.value.code == ACP_INVALID_PARAMS
    assert mismatch.value.data == {"reason": "session_cwd_mismatch"}

    assert loader.capabilities.load_session is True
    assert loader.capabilities.image_prompts is False
    assert loader.capabilities.audio_prompts is False
    assert loader.capabilities.embedded_context is False
    assert loader.capabilities.additional_directories is False
    assert loader.capabilities.mcp_stdio is False
    assert loader.capabilities.mcp_http is False
    assert loader.capabilities.mcp_sse is False


@pytest.mark.asyncio
async def test_prompt_uses_sessionprompt_boundary_and_projects_only_safe_updates(
    session_factory,
    tmp_path: Path,
) -> None:
    instances: list[Any] = []

    class ProjectionPrompt:
        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            self.job = job
            self.request = request
            self.kwargs = kwargs
            self.recorded_external_user_message_id = kwargs.get(
                "external_user_message_id"
            )
            instances.append(self)

        async def run(self) -> None:
            # A checkpoint/lifecycle payload remains observable inside the
            # normal job, but must not be copied onto ACP.
            self.job.publish_lifecycle(
                "checkpoint.created",
                {
                    "checkpoint_id": "checkpoint-1",
                    "raw_hook_output": "secret hook stdout",
                    "private_path": "/private/workspace",
                },
            )
            self.job.publish(
                SSEEvent(
                    TEXT_DELTA,
                    {
                        "message_id": "internal-ulid",
                        "text": "safe answer",
                    },
                )
            )
            self.job.publish(
                SSEEvent(REASONING_DELTA, {"text": "secret chain of thought"})
            )
            self.job.publish(
                SSEEvent(
                    TOOL_START,
                    {
                        "call_id": "provider-secret-call-id",
                        "tool": "bash",
                        "arguments": {"authorization": "Bearer secret"},
                    },
                )
            )
            self.job.publish(
                SSEEvent(
                    TOOL_RESULT,
                    {
                        "call_id": "provider-secret-call-id",
                        "output": "secret command stdout",
                        "metadata": {"api_key": "secret-key"},
                    },
                )
            )
            self.job.publish(
                SSEEvent(
                    DONE,
                    {"finish_reason": "stop", "total_cost": 12.34},
                )
            )

    stream_manager = StreamManager()
    bridge = _bridge(
        session_factory,
        stream_manager,
        prompt_factory=ProjectionPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))
    updates: list[dict[str, Any]] = []

    async def emit(update) -> None:
        payload = dict(update)
        SessionNotification.model_validate(
            {"sessionId": created.session_id, "update": payload}
        )
        updates.append(payload)

    message_id = "8e470df4-1b99-4727-aa63-312e5c1db6f4"
    response = await bridge.prompt(
        _prompt_request(created.session_id, "ACP text", message_id=message_id),
        emit,
    )

    assert response.stop_reason == "end_turn"
    assert response.user_message_id == message_id
    assert len(instances) == 1
    prompt = instances[0]
    assert prompt.request.session_id == created.session_id
    assert prompt.request.text == "ACP text"
    assert prompt.request.workspace == str(tmp_path.resolve())
    assert prompt.request.permission_presets is None
    assert prompt.request.permission_rules is None
    assert prompt.kwargs["skip_user_message"] is False
    assert prompt.kwargs["session_factory"] is session_factory
    assert prompt.job.interactive is False
    assert prompt.job.invocation_source == "acp"
    assert prompt.job.accepting_session_inputs is False
    assert prompt.job.completed is True
    assert any(
        isinstance(event, LifecycleEventV1)
        and event.event_type == "checkpoint.created"
        for event in prompt.job.lifecycle_events
    )

    assert [item["sessionUpdate"] for item in updates] == [
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
    ]
    assert updates[0]["content"]["text"] == "safe answer"
    assert updates[1]["status"] == "in_progress"
    assert updates[2]["status"] == "completed"
    serialized = json.dumps(updates)
    assert "secret" not in serialized
    assert "reasoning" not in serialized
    assert "rawInput" not in serialized
    assert "rawOutput" not in serialized
    assert "bash" not in serialized
    assert "/private" not in serialized


@pytest.mark.asyncio
async def test_default_factory_reaches_real_sessionprompt_run_under_global_admission(
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.session.prompt import SessionPrompt

    stream_manager = StreamManager()
    initial_slots = stream_manager._semaphore._value
    observed: list[SessionPrompt] = []

    async def fake_run(self: SessionPrompt, *, publish_done: bool = True) -> None:
        observed.append(self)
        assert publish_done is True
        assert stream_manager._semaphore._value == initial_slots - 1
        self.job.publish_lifecycle(
            "checkpoint.created",
            {"checkpoint_id": "real-boundary-checkpoint"},
        )
        self.job.publish(
            SSEEvent(
                PERMISSION_REQUEST,
                {
                    "call_id": "private-permission-call",
                    "message": "private permission body",
                },
            )
        )
        await self.job.abort_event.wait()

    monkeypatch.setattr(SessionPrompt, "run", fake_run)
    bridge = ProductionSessionPromptBridge(
        session_factory=session_factory,
        stream_manager=stream_manager,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
        index_manager=object(),
        cancellation_timeout_seconds=1,
    )
    created = await bridge.new_session(_new_request(tmp_path))
    updates: list[dict[str, Any]] = []

    async def emit(update) -> None:
        updates.append(dict(update))

    response = await bridge.prompt(_prompt_request(created.session_id), emit)

    assert response.stop_reason == "refusal"
    assert len(observed) == 1
    assert isinstance(observed[0], SessionPrompt)
    assert observed[0].checkpoint_binding is None
    assert observed[0].job.completed is True
    assert stream_manager._semaphore._value == initial_slots
    assert updates[0]["content"]["text"].startswith("This turn was stopped")


@pytest.mark.parametrize(
    ("signal", "expected_kind", "prompt_type"),
    [
        ("permission", "permission", "permission"),
        ("question", "question", "question"),
        ("plan", "plan_review", "plan"),
        ("hook", "hook_approval", "permission"),
        ("headless_permission", "permission", None),
    ],
)
@pytest.mark.asyncio
async def test_reverse_interactions_fail_closed_with_opaque_structured_refusal(
    session_factory,
    tmp_path: Path,
    signal: str,
    expected_kind: str,
    prompt_type: str | None,
) -> None:
    instances: list[Any] = []

    class InteractivePrompt:
        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            self.job = job
            instances.append(self)

        async def run(self) -> None:
            if prompt_type is not None:
                self.job.register_response_request(
                    "pending-call",
                    prompt_type=prompt_type,
                    timeout=60,
                    tool_call_id="private-tool-call",
                    tool=("hook_command" if signal == "hook" else "private-tool-name"),
                )
            if signal == "permission":
                self.job.publish(
                    SSEEvent(
                        PERMISSION_REQUEST,
                        {
                            "call_id": "pending-call",
                            "arguments": {"api_key": "secret-key"},
                            "message": "secret permission details",
                        },
                    )
                )
            elif signal == "question":
                self.job.publish(
                    SSEEvent(
                        QUESTION,
                        {
                            "call_id": "pending-call",
                            "question": "secret question text",
                        },
                    )
                )
            elif signal == "plan":
                self.job.publish(
                    SSEEvent(
                        PLAN_REVIEW,
                        {
                            "call_id": "pending-call",
                            "plan": "secret plan body",
                            "plan_path": "/private/plan.md",
                        },
                    )
                )
            elif signal == "hook":
                self.job.publish_lifecycle(
                    "hook.dispatch.completed",
                    {
                        "approval_required_count": 1,
                        "raw_hook_output": "secret hook stdout",
                        "command": "secret command",
                    },
                )
                self.job.publish(
                    SSEEvent(
                        PERMISSION_REQUEST,
                        {
                            "call_id": "pending-call",
                            "tool_call_id": "private-tool-call",
                            "arguments": {"command": "secret command"},
                            "message": "secret Hook approval details",
                        },
                    )
                )
            else:
                self.job.publish(
                    SSEEvent(
                        AGENT_ERROR,
                        {
                            "error_type": "permission_required",
                            "error_message": "secret tool and path",
                            "tool": "secret-tool",
                        },
                    )
                )
            await asyncio.wait_for(self.job.abort_event.wait(), timeout=1)

    stream_manager = StreamManager()
    bridge = _bridge(
        session_factory,
        stream_manager,
        prompt_factory=InteractivePrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))
    updates: list[dict[str, Any]] = []

    async def emit(update) -> None:
        payload = dict(update)
        SessionNotification.model_validate(
            {"sessionId": created.session_id, "update": payload}
        )
        updates.append(payload)

    response = await bridge.prompt(_prompt_request(created.session_id), emit)

    assert response.stop_reason == "refusal"
    assert response.field_meta == {
        "suxiaoyou": {
            "code": "acp_reverse_interaction_unavailable",
            "interactionType": expected_kind,
        }
    }
    assert len(instances) == 1
    job = instances[0].job
    assert job.abort_event.is_set()
    assert job.execution_admission_open is False
    if prompt_type is not None:
        pending = job.get_response_request("pending-call")
        assert pending is not None
        assert pending.state == "resolved"
        assert pending.response is False
        assert pending.source == "acp_interaction_unavailable"

    assert len(updates) == 1
    assert updates[0]["sessionUpdate"] == "agent_message_chunk"
    serialized = json.dumps(updates)
    assert "secret" not in serialized
    assert "/private" not in serialized
    assert "pending-call" not in serialized


@pytest.mark.asyncio
async def test_permission_and_separate_hook_approval_round_trip_without_private_data(
    session_factory,
    tmp_path: Path,
) -> None:
    requests = []
    decisions: list[dict[str, bool]] = []
    instances: list[Any] = []

    async def request_permission(request):
        requests.append(request)
        allow = next(option for option in request.options if option.kind == "allow_once")
        return RequestPermissionResponse.model_validate(
            {"outcome": {"outcome": "selected", "optionId": allow.option_id}}
        )

    class TwoPermissionsPrompt:
        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            self.job = job
            instances.append(self)

        async def run(self) -> None:
            ordinary = self.job.register_response_request(
                "private-ordinary-request",
                prompt_type="permission",
                timeout=60,
                tool_call_id="private-tool-call",
                tool="filesystem_write",
            )
            self.job.publish_lifecycle(
                "permission.requested",
                {"path": "/private/report.docx", "secret": "ordinary-secret"},
            )
            self.job.publish(
                SSEEvent(
                    PERMISSION_REQUEST,
                    {
                        "call_id": ordinary.call_id,
                        "tool_call_id": ordinary.tool_call_id,
                        "arguments": {
                            "path": "/private/report.docx",
                            "api_key": "ordinary-secret",
                        },
                        "message": "private ordinary permission body",
                    },
                )
            )
            decisions.append(await self.job.wait_for_response(ordinary.call_id))

            hook = self.job.register_response_request(
                "private-hook-request",
                prompt_type="permission",
                timeout=60,
                tool_call_id="private-tool-call",
                tool="hook_command",
            )
            self.job.publish_lifecycle(
                "hook.approval.required",
                {"command": "curl secret.invalid", "path": "/private/hook"},
            )
            self.job.publish_lifecycle(
                "hook.dispatch.completed",
                {"approval_required_count": 1, "raw_hook_output": "hook-secret"},
            )
            self.job.publish(
                SSEEvent(
                    PERMISSION_REQUEST,
                    {
                        "call_id": hook.call_id,
                        "tool_call_id": hook.tool_call_id,
                        "arguments": {"command": "curl secret.invalid"},
                        "message": "private Hook approval body",
                    },
                )
            )
            decisions.append(await self.job.wait_for_response(hook.call_id))
            self.job.publish(SSEEvent(DONE, {"finish_reason": "stop"}))

    stream_manager = StreamManager()
    bridge = ProductionSessionPromptBridge(
        session_factory=session_factory,
        stream_manager=stream_manager,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
        prompt_factory=TwoPermissionsPrompt,
        permission_requester=request_permission,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    response = await bridge.prompt(_prompt_request(created.session_id), emit)

    assert response.stop_reason == "end_turn"
    assert decisions == [
        {"allowed": True, "remember": False},
        {"allowed": True, "remember": False},
    ]
    assert instances[0].job.interactive is True
    assert len(requests) == 2
    assert all({option.kind for option in request.options} == {"allow_once", "reject_once"} for request in requests)
    serialized = json.dumps(
        [request.model_dump(mode="json", by_alias=True) for request in requests]
    )
    assert "ordinary-secret" not in serialized
    assert "hook-secret" not in serialized
    assert "curl" not in serialized
    assert "/private" not in serialized
    assert "private-tool-call" not in serialized
    assert "private-ordinary-request" not in serialized
    assert "private-hook-request" not in serialized


@pytest.mark.asyncio
async def test_reverse_requester_failure_resolves_exact_permission_as_denied(
    session_factory,
    tmp_path: Path,
) -> None:
    instances: list[Any] = []

    async def failed_requester(_request):
        raise RuntimeError("private client failure")

    class PermissionPrompt:
        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            self.job = job
            instances.append(self)

        async def run(self) -> None:
            record = self.job.register_response_request(
                "permission-request",
                prompt_type="permission",
                timeout=60,
                tool_call_id="tool-call",
                tool="filesystem_write",
            )
            self.job.publish(
                SSEEvent(PERMISSION_REQUEST, {"call_id": record.call_id})
            )
            decision = await self.job.wait_for_response(record.call_id)
            assert decision is False
            self.job.publish(SSEEvent(DONE, {"finish_reason": "stop"}))

    bridge = ProductionSessionPromptBridge(
        session_factory=session_factory,
        stream_manager=StreamManager(),
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
        prompt_factory=PermissionPrompt,
        permission_requester=failed_requester,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    response = await bridge.prompt(_prompt_request(created.session_id), emit)
    record = instances[0].job.get_response_request("permission-request")

    assert response.stop_reason == "end_turn"
    assert record is not None
    assert record.state == "resolved"
    assert record.source == "acp_reverse_permission_failed"


@pytest.mark.asyncio
async def test_wire_cancel_during_reverse_permission_denies_before_abort(
    session_factory,
    tmp_path: Path,
) -> None:
    instances: list[Any] = []
    decisions: list[Any] = []

    class WaitingPermissionPrompt:
        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            self.job = job
            instances.append(self)

        async def run(self) -> None:
            record = self.job.register_response_request(
                "private-cancel-request",
                prompt_type="permission",
                timeout=60,
                tool_call_id="private-cancel-tool",
                tool="filesystem_write",
            )
            self.job.publish(
                SSEEvent(
                    PERMISSION_REQUEST,
                    {
                        "call_id": record.call_id,
                        "arguments": {"path": "/private/never-written"},
                    },
                )
            )
            decisions.append(await self.job.wait_for_response(record.call_id))

    bridge = ProductionSessionPromptBridge(
        session_factory=session_factory,
        stream_manager=StreamManager(),
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
        prompt_factory=WaitingPermissionPrompt,
        cancellation_timeout_seconds=1,
    )
    wire = await WireHarness(bridge).start()
    try:
        await wire.initialize()
        session_id = await wire.new_session(cwd=str(tmp_path))
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": "cancel-prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "run"}],
                },
            }
        )
        messages = await wire.writer.wait_for_count(3)
        assert any(
            message.get("method") == "session/request_permission"
            for message in messages
        )
        wire.send(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
            }
        )
        response = await wire.response("cancel-prompt")
        record = instances[0].job.get_response_request("private-cancel-request")

        assert response["result"]["stopReason"] == "cancelled"
        assert decisions == [False]
        assert record is not None
        assert record.state == "resolved"
        assert record.source in {
            "acp_reverse_permission_failed",
            "acp_cancel",
        }
        assert instances[0].job.abort_event.is_set()
        assert wire.server._pending_client_requests == {}
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_same_session_concurrency_and_foreign_stream_fail_closed(
    session_factory,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    instances: list[Any] = []

    class BlockingPrompt:
        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            self.job = job
            instances.append(self)

        async def run(self) -> None:
            started.set()
            await self.job.abort_event.wait()

    stream_manager = StreamManager()
    bridge = _bridge(
        session_factory,
        stream_manager,
        prompt_factory=BlockingPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    first = asyncio.create_task(
        bridge.prompt(_prompt_request(created.session_id, "first"), emit)
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    with pytest.raises(BridgeRpcError) as concurrent:
        await bridge.prompt(_prompt_request(created.session_id, "second"), emit)
    assert concurrent.value.code == ACP_SERVER_BUSY
    assert concurrent.value.data == {"reason": "session_prompt_already_active"}

    await bridge.cancel(created.session_id)
    assert (await asyncio.wait_for(first, timeout=1)).stop_reason == "cancelled"
    assert instances[0].job.completed is True

    foreign = stream_manager.create_job(
        stream_id="foreign-desktop-stream",
        session_id=created.session_id,
        invocation_source="desktop",
    )
    with pytest.raises(BridgeRpcError) as occupied:
        await bridge.prompt(_prompt_request(created.session_id, "third"), emit)
    assert occupied.value.code == ACP_SERVER_BUSY
    assert foreign.abort_event.is_set() is False
    foreign.complete()


@pytest.mark.asyncio
async def test_disconnect_cancels_only_connection_owned_turn_and_keeps_sessions(
    session_factory,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()

    class BlockingPrompt:
        instances: list[Any] = []

        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            self.job = job
            self.__class__.instances.append(self)

        async def run(self) -> None:
            started.set()
            await self.job.abort_event.wait()

    stream_manager = StreamManager()
    bridge = _bridge(
        session_factory,
        stream_manager,
        prompt_factory=BlockingPrompt,
    )
    owned = await bridge.new_session(_new_request(tmp_path))

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    unrelated = await bridge.new_session(_new_request(second_dir))
    foreign = stream_manager.create_job(
        stream_id="foreign-connection-stream",
        session_id=unrelated.session_id,
        invocation_source="desktop",
    )

    async def emit(_update) -> None:
        return None

    prompt_task = asyncio.create_task(
        bridge.prompt(_prompt_request(owned.session_id), emit)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await bridge.disconnect((owned.session_id, unrelated.session_id))
    response = await asyncio.wait_for(prompt_task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert BlockingPrompt.instances[0].job.abort_event.is_set()
    assert foreign.abort_event.is_set() is False
    async with session_factory() as db:
        assert await get_session(db, owned.session_id) is not None
        assert await get_session(db, unrelated.session_id) is not None
    foreign.complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_id",
    [None, "13cff18f-861b-4096-8aac-8214f5ca980f"],
)
async def test_cancel_during_session_preflight_is_not_lost(
    session_factory,
    tmp_path: Path,
    message_id: str | None,
) -> None:
    factory_called = False

    class ShouldNotRunPrompt:
        def __init__(self, job: GenerationJob, request, **kwargs: Any) -> None:
            nonlocal factory_called
            factory_called = True

        async def run(self) -> None:  # pragma: no cover - constructor must not run
            raise AssertionError("cancelled prompt reached SessionPrompt.run")

    stream_manager = StreamManager()
    bridge = _bridge(
        session_factory,
        stream_manager,
        prompt_factory=ShouldNotRunPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))
    preflight_entered = asyncio.Event()
    release_preflight = asyncio.Event()
    original = bridge._read_bound_session

    async def delayed_read(*args: Any, **kwargs: Any):
        preflight_entered.set()
        await release_preflight.wait()
        return await original(*args, **kwargs)

    bridge._read_bound_session = delayed_read  # type: ignore[method-assign]

    async def emit(_update) -> None:
        return None

    task = asyncio.create_task(
        bridge.prompt(
            _prompt_request(created.session_id, message_id=message_id),
            emit,
        )
    )
    await asyncio.wait_for(preflight_entered.wait(), timeout=1)
    await bridge.cancel(created.session_id)
    release_preflight.set()
    response = await asyncio.wait_for(task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert response.user_message_id is None
    assert factory_called is False
    if message_id is not None:
        async with session_factory() as db:
            record = (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope
                        == f"acp.prompt:{created.session_id}"
                    )
                )
            ).scalar_one()
        assert record.status == "interrupted"
        assert record.response == {"stopReason": "cancelled"}


@pytest.mark.asyncio
async def test_cancel_keeps_session_busy_until_stubborn_tool_really_stops(
    session_factory,
    tmp_path: Path,
) -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    class StubbornToolPrompt:
        def __init__(self, job: GenerationJob, _request, **_kwargs: Any) -> None:
            self.job = job

        async def run(self) -> None:
            async def stubborn_tool() -> None:
                tool_started.set()
                while not release_tool.is_set():
                    try:
                        await release_tool.wait()
                    except asyncio.CancelledError:
                        # Model native/subprocess cleanup that remains live
                        # across repeated cancellation attempts.
                        continue

            tool = asyncio.create_task(stubborn_tool())
            self.job.track_tool_task(tool)
            await self.job.abort_event.wait()

    manager = StreamManager()
    bridge = _bridge(
        session_factory,
        manager,
        prompt_factory=StubbornToolPrompt,
        cancellation_timeout_seconds=0.02,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    prompt_task = asyncio.create_task(
        bridge.prompt(_prompt_request(created.session_id), emit)
    )
    await asyncio.wait_for(tool_started.wait(), timeout=1)
    cancel_task = asyncio.create_task(bridge.cancel(created.session_id))
    await asyncio.sleep(0.06)

    assert not cancel_task.done()
    assert not prompt_task.done()
    assert manager.active_job_for_session(created.session_id) is not None
    with pytest.raises(SessionBusyError):
        manager.create_job("replacement-too-early", created.session_id)

    release_tool.set()
    await asyncio.wait_for(cancel_task, timeout=1)
    response = await asyncio.wait_for(prompt_task, timeout=1)
    assert response.stop_reason == "cancelled"
    assert manager.active_job_for_session(created.session_id) is None
    replacement = manager.create_job("replacement-after-quiescence", created.session_id)
    replacement.complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_id",
    [
        "not-a-uuid",
        "8E470DF4-1B99-4727-AA63-312E5C1DB6F4",
        "{8e470df4-1b99-4727-aa63-312e5c1db6f4}",
    ],
)
async def test_prompt_rejects_noncanonical_message_id_before_execution(
    session_factory,
    tmp_path: Path,
    message_id: str,
) -> None:
    factory_called = False

    class ShouldNotRunPrompt:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal factory_called
            factory_called = True

    bridge = _bridge(
        session_factory,
        StreamManager(),
        prompt_factory=ShouldNotRunPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    with pytest.raises(BridgeRpcError) as invalid:
        await bridge.prompt(
            _prompt_request(created.session_id, message_id=message_id),
            emit,
        )

    assert invalid.value.code == ACP_INVALID_PARAMS
    assert invalid.value.data == {
        "reason": "message_id_must_be_canonical_uuid"
    }
    assert factory_called is False
    async with session_factory() as db:
        records = list((await db.execute(select(IdempotencyRecord))).scalars())
    assert records == []


@pytest.mark.asyncio
async def test_keyless_prompts_remain_compatible_and_do_not_create_ledger_rows(
    session_factory,
    tmp_path: Path,
) -> None:
    executions = 0

    class CountingPrompt(_NoopPrompt):
        async def run(self) -> None:
            nonlocal executions
            executions += 1
            await super().run()

    bridge = _bridge(
        session_factory,
        StreamManager(),
        prompt_factory=CountingPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    first = await bridge.prompt(_prompt_request(created.session_id, "same"), emit)
    second = await bridge.prompt(_prompt_request(created.session_id, "same"), emit)

    assert first.stop_reason == second.stop_reason == "end_turn"
    assert executions == 2
    async with session_factory() as db:
        records = list((await db.execute(select(IdempotencyRecord))).scalars())
    assert records == []


@pytest.mark.asyncio
async def test_completed_prompt_replays_across_bridge_restart_without_execution(
    session_factory,
    tmp_path: Path,
) -> None:
    executions = 0
    message_id = "8e470df4-1b99-4727-aa63-312e5c1db6f4"

    class CountingPrompt(_NoopPrompt):
        async def run(self) -> None:
            nonlocal executions
            executions += 1
            await super().run()

    first_bridge = _bridge(
        session_factory,
        StreamManager(),
        prompt_factory=CountingPrompt,
    )
    created = await first_bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    first_request = PromptRequest.model_validate(
        {
            "sessionId": created.session_id,
            "messageId": message_id,
            "_meta": {"trace": "first-transport-metadata"},
            "prompt": [{"type": "text", "text": "durable prompt"}],
        }
    )
    first = await first_bridge.prompt(first_request, emit)

    async with session_factory() as db:
        record = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope
                    == f"acp.prompt:{created.session_id}",
                    IdempotencyRecord.request_key == message_id,
                )
            )
        ).scalar_one()
    assert record.status == "completed"
    assert record.error_message is None
    assert record.response == {
        "stopReason": "end_turn",
        "userMessageId": message_id,
    }
    assert str(tmp_path.resolve()) not in json.dumps(record.response)

    # A new bridge and StreamManager simulate a process-local runtime restart;
    # only the database record survives.
    replay_executed = False

    class ReplayMustNotRun:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal replay_executed
            replay_executed = True

    restarted = _bridge(
        session_factory,
        StreamManager(),
        prompt_factory=ReplayMustNotRun,
    )
    await restarted.load_session(
        _load_request(created.session_id, tmp_path),
        emit,
    )
    replay_request = PromptRequest.model_validate(
        {
            "sessionId": created.session_id,
            "messageId": message_id,
            "_meta": {"trace": "different-transport-metadata"},
            "prompt": [{"type": "text", "text": "durable prompt"}],
        }
    )
    replay = await restarted.prompt(replay_request, emit)

    assert replay == first
    assert replay.user_message_id == message_id
    assert executions == 1
    assert replay_executed is False

    with pytest.raises(BridgeRpcError) as conflict:
        await restarted.prompt(
            _prompt_request(
                created.session_id,
                "different effective prompt",
                message_id=message_id,
            ),
            emit,
        )
    assert conflict.value.code == ACP_INVALID_PARAMS
    assert conflict.value.data == {"reason": "idempotency_conflict"}
    assert executions == 1
    assert replay_executed is False


@pytest.mark.asyncio
async def test_keyed_prompt_converges_across_connections_and_cancel_is_terminal(
    session_factory,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    executions = 0
    message_id = "7f15f9bd-1f9f-4dcc-8462-bdb66ac5cbc7"

    class BlockingPrompt:
        def __init__(self, job: GenerationJob, _request, **_kwargs: Any) -> None:
            self.job = job

        async def run(self) -> None:
            nonlocal executions
            executions += 1
            started.set()
            await self.job.abort_event.wait()

    manager = StreamManager()
    owner = _bridge(
        session_factory,
        manager,
        prompt_factory=BlockingPrompt,
    )
    created = await owner.new_session(_new_request(tmp_path))
    retrying_connection = _bridge(
        session_factory,
        manager,
        prompt_factory=BlockingPrompt,
    )

    async def emit(_update) -> None:
        return None

    await retrying_connection.load_session(
        _load_request(created.session_id, tmp_path), emit
    )
    request = _prompt_request(
        created.session_id,
        "execute once",
        message_id=message_id,
    )
    owner_task = asyncio.create_task(owner.prompt(request, emit))
    await asyncio.wait_for(started.wait(), timeout=1)

    async with session_factory() as db:
        running = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope
                    == f"acp.prompt:{created.session_id}",
                    IdempotencyRecord.request_key == message_id,
                )
            )
        ).scalar_one()
    assert running.status == "running"

    with pytest.raises(BridgeRpcError) as in_flight:
        await retrying_connection.prompt(request, emit)
    assert in_flight.value.code == ACP_SERVER_BUSY
    assert in_flight.value.data == {
        "reason": "idempotency_in_flight",
        "status": "running",
    }
    assert executions == 1

    await owner.cancel(created.session_id)
    response = await asyncio.wait_for(owner_task, timeout=1)
    assert response.stop_reason == "cancelled"

    async with session_factory() as db:
        interrupted = await db.get(IdempotencyRecord, running.id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    assert interrupted.error_message == "acp_prompt_interrupted"

    with pytest.raises(BridgeRpcError) as terminal:
        await retrying_connection.prompt(request, emit)
    assert terminal.value.code == ACP_IDEMPOTENCY_REJECTED
    assert terminal.value.data == {"reason": "idempotency_interrupted"}
    assert executions == 1


@pytest.mark.asyncio
async def test_terminal_ledger_db_failure_is_owned_until_reconciled(
    session_factory,
    tmp_path: Path,
) -> None:
    manager = StreamManager()
    bridge = _bridge(
        session_factory,
        manager,
        prompt_factory=_NoopPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))
    message_id = "1767f1d4-87f8-48fc-a636-504bd232bb34"
    release_terminal_write = asyncio.Event()
    terminal_attempts = 0
    transition = bridge._transition_prompt_ledger  # type: ignore[attr-defined]

    async def flaky_transition(*args: Any, **kwargs: Any) -> bool:
        nonlocal terminal_attempts
        if (
            kwargs.get("status") == "completed"
            and not release_terminal_write.is_set()
        ):
            terminal_attempts += 1
            raise RuntimeError("private database error at /secret/workspace")
        return await transition(*args, **kwargs)

    bridge._transition_prompt_ledger = flaky_transition  # type: ignore[method-assign]

    async def emit(_update) -> None:
        return None

    response = await asyncio.wait_for(
        bridge.prompt(
            _prompt_request(
                created.session_id,
                "execute once and reconcile",
                message_id=message_id,
            ),
            emit,
        ),
        timeout=1,
    )
    assert response.stop_reason == "end_turn"
    assert response.user_message_id == message_id
    assert terminal_attempts >= 3
    assert manager._reconciliation_tasks

    async with session_factory() as db:
        pending = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope
                    == f"acp.prompt:{created.session_id}",
                    IdempotencyRecord.request_key == message_id,
                )
            )
        ).scalar_one()
    assert pending.status == "running"

    release_terminal_write.set()
    assert await manager.wait_for_reconciliation_tasks(1.0) is True
    assert not manager._reconciliation_tasks
    async with session_factory() as db:
        completed = await db.get(IdempotencyRecord, pending.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.response == {
        "stopReason": "end_turn",
        "userMessageId": message_id,
    }


@pytest.mark.asyncio
async def test_completed_ledger_without_bound_message_never_replays(
    session_factory,
    tmp_path: Path,
) -> None:
    message_id = "42b7c2d7-fbca-49cc-95a4-aacac61174b7"
    prompt_text = "missing durable user message"
    bridge = _bridge(
        session_factory,
        StreamManager(),
        prompt_factory=lambda *_args, **_kwargs: pytest.fail(
            "invalid replay executed"
        ),
    )
    created = await bridge.new_session(_new_request(tmp_path))
    async with session_factory() as db:
        async with db.begin():
            db.add(
                IdempotencyRecord(
                    scope=f"acp.prompt:{created.session_id}",
                    request_key=message_id,
                    request_hash=bridge._prompt_request_hash(  # type: ignore[attr-defined]
                        session_id=created.session_id,
                        text=prompt_text,
                    ),
                    status="completed",
                    response={
                        "stopReason": "end_turn",
                        "userMessageId": message_id,
                    },
                )
            )

    async def emit(_update) -> None:
        return None

    with pytest.raises(BridgeRpcError) as rejected:
        await bridge.prompt(
            _prompt_request(
                created.session_id,
                prompt_text,
                message_id=message_id,
            ),
            emit,
        )
    assert rejected.value.code == ACP_IDEMPOTENCY_REJECTED
    assert rejected.value.data == {
        "reason": "idempotency_message_binding_invalid"
    }


@pytest.mark.asyncio
async def test_accepted_prompt_record_reports_in_flight_without_execution(
    session_factory,
    tmp_path: Path,
) -> None:
    message_id = "843f2775-cf47-4f04-8b96-446dbab867e2"
    prompt_text = "already admitted"
    bridge = _bridge(
        session_factory,
        StreamManager(),
        prompt_factory=lambda *_args, **_kwargs: pytest.fail(
            "accepted replay executed"
        ),
    )
    created = await bridge.new_session(_new_request(tmp_path))
    request_hash = bridge._prompt_request_hash(  # type: ignore[attr-defined]
        session_id=created.session_id,
        text=prompt_text,
    )
    async with session_factory() as db:
        async with db.begin():
            db.add(
                IdempotencyRecord(
                    scope=f"acp.prompt:{created.session_id}",
                    request_key=message_id,
                    request_hash=request_hash,
                    status="accepted",
                    response={},
                )
            )

    async def emit(_update) -> None:
        return None

    with pytest.raises(BridgeRpcError) as in_flight:
        await bridge.prompt(
            _prompt_request(
                created.session_id,
                prompt_text,
                message_id=message_id,
            ),
            emit,
        )
    assert in_flight.value.code == ACP_SERVER_BUSY
    assert in_flight.value.data == {
        "reason": "idempotency_in_flight",
        "status": "accepted",
    }


@pytest.mark.asyncio
async def test_failed_keyed_prompt_is_durable_and_never_reexecuted(
    session_factory,
    tmp_path: Path,
) -> None:
    executions = 0
    message_id = "6c434bc2-894b-44db-8b36-e1d6ff50b8a9"

    class FailingPrompt:
        def __init__(self, _job: GenerationJob, _request, **_kwargs: Any) -> None:
            pass

        async def run(self) -> None:
            nonlocal executions
            executions += 1
            raise RuntimeError("secret failure at /private/workspace")

    bridge = _bridge(
        session_factory,
        StreamManager(),
        prompt_factory=FailingPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    request = _prompt_request(
        created.session_id,
        "fail once",
        message_id=message_id,
    )
    first = await bridge.prompt(request, emit)
    assert first.stop_reason == "refusal"

    async with session_factory() as db:
        record = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope
                    == f"acp.prompt:{created.session_id}"
                )
            )
        ).scalar_one()
    assert record.status == "failed"
    assert record.error_message == "acp_prompt_failed"
    serialized = json.dumps(record.response)
    assert "secret" not in serialized
    assert "/private" not in serialized

    with pytest.raises(BridgeRpcError) as terminal:
        await bridge.prompt(request, emit)
    assert terminal.value.code == ACP_IDEMPOTENCY_REJECTED
    assert terminal.value.data == {"reason": "idempotency_failed"}
    assert executions == 1


@pytest.mark.asyncio
async def test_session_deletion_removes_bound_acp_prompt_ledger(
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_id = "4b1215bb-cc0a-4f85-811c-d19a6b75ec1d"
    manager = StreamManager()
    bridge = _bridge(
        session_factory,
        manager,
        prompt_factory=_NoopPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def emit(_update) -> None:
        return None

    await bridge.prompt(
        _prompt_request(
            created.session_id,
            "delete with session",
            message_id=message_id,
        ),
        emit,
    )
    monkeypatch.setattr("app.dependencies.get_index_manager", lambda: None)
    await delete_session_cascade(created.session_id, manager, session_factory)

    async with session_factory() as db:
        records = list(
            (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope
                        == f"acp.prompt:{created.session_id}"
                    )
                )
            ).scalars()
        )
    assert records == []


@pytest.mark.asyncio
async def test_session_delete_fails_closed_when_execution_is_not_quiescent(
    session_factory,
    tmp_path: Path,
) -> None:
    manager = StreamManager()
    bridge = _bridge(
        session_factory,
        manager,
        prompt_factory=_NoopPrompt,
    )
    created = await bridge.new_session(_new_request(tmp_path))

    async def non_quiescent_abort(
        _session_id: str,
        *,
        timeout: float,
    ) -> tuple[int, bool]:
        assert timeout == 10.0
        return 1, False

    manager.abort_session_and_wait = non_quiescent_abort  # type: ignore[method-assign]

    with pytest.raises(Conflict, match="blocked until all running work has stopped"):
        await delete_session_cascade(created.session_id, manager, session_factory)

    async with session_factory() as db:
        assert await get_session(db, created.session_id) is not None


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_global_admission_is_not_lost(
    session_factory,
    tmp_path: Path,
) -> None:
    manager = StreamManager()
    prompt_factory_called = False

    class MustNotRun:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal prompt_factory_called
            prompt_factory_called = True

    bridge = _bridge(session_factory, manager, prompt_factory=MustNotRun)
    created = await bridge.new_session(_new_request(tmp_path))
    message_id = "bc1364f6-325c-4af1-bb2b-a0e4de1cb2cd"

    async def emit(_update) -> None:
        return None

    await manager.job_admission_lock.acquire()
    task = asyncio.create_task(
        bridge.prompt(
            _prompt_request(
                created.session_id,
                "cancel before admission",
                message_id=message_id,
            ),
            emit,
        )
    )
    try:
        async def claimed() -> None:
            while created.session_id not in bridge._claimed_sessions:
                await asyncio.sleep(0)

        await asyncio.wait_for(claimed(), timeout=1)
        await bridge.cancel(created.session_id)
    finally:
        manager.job_admission_lock.release()

    response = await asyncio.wait_for(task, timeout=1)
    assert response.stop_reason == "cancelled"
    assert prompt_factory_called is False
    async with session_factory() as db:
        record = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope
                    == f"acp.prompt:{created.session_id}",
                    IdempotencyRecord.request_key == message_id,
                )
            )
        ).scalar_one()
    assert record.status == "interrupted"


@pytest.mark.asyncio
async def test_committed_session_delete_wins_over_waiting_acp_admission(
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = StreamManager()
    prompt_factory_called = False

    class MustNotRun:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal prompt_factory_called
            prompt_factory_called = True

    bridge = _bridge(session_factory, manager, prompt_factory=MustNotRun)
    created = await bridge.new_session(_new_request(tmp_path))
    message_id = "32768bb0-f30a-4d72-a496-957cb1160073"
    monkeypatch.setattr("app.dependencies.get_index_manager", lambda: None)

    async def emit(_update) -> None:
        return None

    await manager.job_admission_lock.acquire()
    delete_task = asyncio.create_task(
        delete_session_cascade(created.session_id, manager, session_factory)
    )
    # Queue deletion first; the prompt may claim locally, but cannot perform
    # persistent preflight until the committed delete releases admission.
    await asyncio.sleep(0)
    prompt_task = asyncio.create_task(
        bridge.prompt(
            _prompt_request(
                created.session_id,
                "must not resurrect",
                message_id=message_id,
            ),
            emit,
        )
    )
    try:
        async def claimed() -> None:
            while created.session_id not in bridge._claimed_sessions:
                await asyncio.sleep(0)

        await asyncio.wait_for(claimed(), timeout=1)
    finally:
        manager.job_admission_lock.release()

    assert await asyncio.wait_for(delete_task, timeout=1) == {"deleted": True}
    with pytest.raises(BridgeRpcError) as missing:
        await asyncio.wait_for(prompt_task, timeout=1)
    assert missing.value.code == -32002
    assert prompt_factory_called is False

    async with session_factory() as db:
        assert await get_session(db, created.session_id) is None
        records = list(
            (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope
                        == f"acp.prompt:{created.session_id}"
                    )
                )
            ).scalars()
        )
    assert records == []
