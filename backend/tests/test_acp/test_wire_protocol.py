from __future__ import annotations

import asyncio
import json

from acp.meta import AGENT_METHODS, PROTOCOL_VERSION
from acp.schema import RequestPermissionRequest, RequestPermissionResponse
import pytest

from app.acp import AcpLimits, AcpServer, BridgeCapabilities
from app.acp.server import (
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RESOURCE_NOT_FOUND,
    SERVER_BUSY,
    SESSION_LIMIT_REACHED,
)
from app.acp.bridge import ReversePermissionUnavailable

from .conftest import CaptureWriter, RecordingBridge, WireHarness


class ReversePermissionBridge(RecordingBridge):
    def __init__(self) -> None:
        super().__init__()
        self.permission_requester = None
        self.permission_response = None
        self.permission_failed = False
        self.permission_started = asyncio.Event()

    def bind_permission_requester(self, requester) -> None:
        self.permission_requester = requester

    async def prompt(self, request, emit_update):
        del emit_update
        assert self.permission_requester is not None
        self.prompt_started.set()
        self.permission_started.set()
        private = RequestPermissionRequest.model_validate(
            {
                "sessionId": request.session_id,
                "options": [
                    {
                        "optionId": "private-allow-secret",
                        "kind": "allow_once",
                        "name": "allow /private/secret.txt",
                    },
                    {
                        "optionId": "private-reject-secret",
                        "kind": "reject_once",
                        "name": "reject private command",
                    },
                ],
                "toolCall": {
                    "toolCallId": "private-tool-call-id",
                    "title": "curl -H 'Authorization: secret' private.invalid",
                    "kind": "execute",
                    "locations": [{"path": "/private/secret.txt"}],
                    "rawInput": {"api_key": "secret"},
                    "rawOutput": "secret output",
                },
            }
        )
        try:
            self.permission_response = await self.permission_requester(private)
        except ReversePermissionUnavailable:
            self.permission_failed = True
            return {"stopReason": "refusal"}
        return {"stopReason": "end_turn"}


def test_official_and_grok_method_names_are_the_acp_v1_wire() -> None:
    assert PROTOCOL_VERSION == 1
    assert {
        AGENT_METHODS["initialize"],
        AGENT_METHODS["authenticate"],
        AGENT_METHODS["session_new"],
        AGENT_METHODS["session_load"],
        AGENT_METHODS["session_prompt"],
        AGENT_METHODS["session_cancel"],
    } == {
        "initialize",
        "authenticate",
        "session/new",
        "session/load",
        "session/prompt",
        "session/cancel",
    }


@pytest.mark.asyncio
async def test_initialize_negotiates_v1_and_advertises_only_backed_capabilities(wire) -> None:
    response = await wire.initialize("init-uuid")

    assert response == {
        "jsonrpc": "2.0",
        "id": "init-uuid",
        "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "agentInfo": {
                "name": "suxiaoyou",
                "title": "苏小有",
                "version": "1.1.0",
            },
            "authMethods": [],
        },
    }
    assert len(wire.bridge.initialized) == 1
    assert wire.bridge.initialized[0].protocol_version == 1


@pytest.mark.asyncio
async def test_initialize_selects_v1_from_newer_client_and_rejects_no_common_version() -> None:
    bridge = RecordingBridge()
    wire = await WireHarness(bridge).start()
    try:
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 2, "clientCapabilities": {}},
            }
        )
        assert (await wire.response(1))["result"]["protocolVersion"] == 1
    finally:
        await wire.close()

    incompatible = await WireHarness(RecordingBridge()).start()
    try:
        incompatible.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": 0, "clientCapabilities": {}},
            }
        )
        response = await incompatible.response(2)
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["data"] == {
            "reason": "unsupported_protocol_version",
            "supported": [1],
        }
    finally:
        await incompatible.close()


@pytest.mark.asyncio
async def test_new_load_and_prompt_use_schema_and_order_updates_before_responses(wire) -> None:
    await wire.initialize()
    session_id = await wire.new_session()
    assert session_id == "session-1"

    wire.send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/load",
            "params": {
                "sessionId": "persisted-1",
                "cwd": "/tmp/project",
                "mcpServers": [],
            },
        }
    )
    load_response = await wire.response(2)
    assert load_response["result"] == {}

    wire.send(
        {
            "jsonrpc": "2.0",
            "id": "prompt-uuid",
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "messageId": "user-1",
                "prompt": [{"type": "text", "text": "hi"}],
            },
        }
    )
    prompt_response = await wire.response("prompt-uuid")
    assert prompt_response["result"] == {"stopReason": "end_turn"}

    messages = wire.writer.messages()
    history_index = next(
        index
        for index, message in enumerate(messages)
        if message.get("params", {}).get("sessionId") == "persisted-1"
    )
    load_index = messages.index(load_response)
    answer_index = next(
        index
        for index, message in enumerate(messages)
        if message.get("params", {}).get("sessionId") == session_id
    )
    prompt_index = messages.index(prompt_response)
    assert history_index < load_index
    assert answer_index < prompt_index
    assert messages[answer_index]["method"] == "session/update"


@pytest.mark.asyncio
async def test_reverse_permission_uses_official_wire_and_server_owned_safe_fields() -> None:
    bridge = ReversePermissionBridge()
    wire = await WireHarness(bridge).start()
    try:
        await wire.initialize()
        session_id = await wire.new_session()
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": "prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "run"}],
                },
            }
        )
        messages = await wire.writer.wait_for_count(3)
        reverse = next(
            message
            for message in messages
            if message.get("method") == "session/request_permission"
        )
        assert isinstance(reverse["id"], str)
        assert reverse["id"].startswith("sxy-permission-")
        request = RequestPermissionRequest.model_validate(reverse["params"])
        assert request.session_id == session_id
        assert request.tool_call.title == "Permission required"
        assert request.tool_call.kind == "other"
        assert request.tool_call.status == "pending"
        assert request.tool_call.content is None
        assert request.tool_call.locations is None
        assert request.tool_call.raw_input is None
        assert request.tool_call.raw_output is None
        assert [option.option_id for option in request.options] == [
            "option-1",
            "option-2",
        ]
        assert [option.name for option in request.options] == [
            "Allow once",
            "Reject once",
        ]
        serialized = json.dumps(reverse)
        assert "secret" not in serialized
        assert "/private" not in serialized
        assert "curl" not in serialized
        assert "private-tool-call-id" not in serialized

        # An unknown response frame cannot resolve the live request.
        wire.send(
            {"jsonrpc": "2.0", "id": "unknown-server-id", "result": {}}
        )
        await asyncio.sleep(0)
        assert bridge.permission_response is None

        wire.send(
            {
                "jsonrpc": "2.0",
                "id": reverse["id"],
                "result": {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": "option-1",
                    }
                },
            }
        )
        prompt = await wire.response("prompt")
        assert prompt["result"]["stopReason"] == "end_turn"
        assert isinstance(bridge.permission_response, RequestPermissionResponse)
        assert bridge.permission_response.outcome.outcome == "selected"
        assert bridge.permission_response.outcome.option_id == "private-allow-secret"

        # A duplicate response is ignored and cannot create a second grant.
        before = len(wire.writer.messages())
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": reverse["id"],
                "result": {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": "option-2",
                    }
                },
            }
        )
        await asyncio.sleep(0.01)
        assert len(wire.writer.messages()) == before
        assert bridge.permission_response.outcome.option_id == "private-allow-secret"
    finally:
        await wire.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_response",
    [
        {"result": {}, "error": {"code": -1, "message": "both"}},
        {"result": {"outcome": {"outcome": "selected", "optionId": "unknown"}}},
        {"result": {"outcome": {"outcome": "selected"}}},
    ],
)
async def test_malformed_reverse_response_fails_permission_closed(bad_response) -> None:
    bridge = ReversePermissionBridge()
    wire = await WireHarness(bridge).start()
    try:
        await wire.initialize()
        session_id = await wire.new_session()
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "run"}],
                },
            }
        )
        messages = await wire.writer.wait_for_count(3)
        reverse = next(
            message
            for message in messages
            if message.get("method") == "session/request_permission"
        )
        wire.send({"jsonrpc": "2.0", "id": reverse["id"], **bad_response})
        response = await wire.response(10)
        assert response["result"]["stopReason"] == "refusal"
        assert bridge.permission_failed is True
        assert bridge.permission_response is None
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_reverse_permission_timeout_is_bounded_and_fails_closed() -> None:
    bridge = ReversePermissionBridge()
    wire = await WireHarness(
        bridge,
        limits=AcpLimits(reverse_request_timeout_seconds=0.01),
    ).start()
    try:
        await wire.initialize()
        session_id = await wire.new_session()
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "run"}],
                },
            }
        )
        response = await wire.response(11)
        assert response["result"]["stopReason"] == "refusal"
        assert bridge.permission_failed is True
        assert wire.server._pending_client_requests == {}
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_reverse_permission_pending_count_and_persistent_choices_are_bounded() -> None:
    server = AcpServer(
        RecordingBridge(),
        limits=AcpLimits(
            max_pending_client_requests=1,
            reverse_request_timeout_seconds=1,
        ),
        enabled=True,
    )
    writer = CaptureWriter()
    server._writer = writer
    server._initialized = True
    server._authenticated = True
    current = asyncio.current_task()
    assert current is not None
    server._active_prompts["session-1"] = current
    request = RequestPermissionRequest.model_validate(
        {
            "sessionId": "session-1",
            "options": [
                {
                    "optionId": "allow-private",
                    "kind": "allow_once",
                    "name": "allow private",
                },
                {
                    "optionId": "reject-private",
                    "kind": "reject_once",
                    "name": "reject private",
                },
            ],
            "toolCall": {"toolCallId": "private-call"},
        }
    )

    first = asyncio.create_task(server.request_permission(request))
    messages = await writer.wait_for_count(1)
    outbound = messages[0]
    with pytest.raises(ReversePermissionUnavailable):
        await server.request_permission(request)

    await server._accept_frame(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": outbound["id"],
                    "result": {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": "option-2",
                        }
                    },
                }
            )
            + "\n"
        ).encode()
    )
    response = await first
    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "reject-private"
    assert server._pending_client_requests == {}

    persistent = RequestPermissionRequest.model_validate(
        {
            "sessionId": "session-1",
            "options": [
                {
                    "optionId": "always-private",
                    "kind": "allow_always",
                    "name": "always private",
                }
            ],
            "toolCall": {"toolCallId": "private-call"},
        }
    )
    with pytest.raises(ReversePermissionUnavailable):
        await server.request_permission(persistent)
    server._active_prompts.clear()


@pytest.mark.asyncio
async def test_eof_clears_pending_reverse_permission_and_unbinds_requester() -> None:
    bridge = ReversePermissionBridge()
    wire = await WireHarness(bridge).start()
    await wire.initialize()
    session_id = await wire.new_session()
    wire.send(
        {
            "jsonrpc": "2.0",
            "id": "pending-at-eof",
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

    await wire.close()

    assert wire.server._pending_client_requests == {}
    assert bridge.permission_requester is None
    assert bridge.cancelled == [session_id]
    assert bridge.disconnected == (session_id,)


@pytest.mark.asyncio
async def test_cancel_is_a_notification_and_prompt_finishes_cancelled_in_wire_order(wire) -> None:
    wire.bridge.block_prompt = True
    await wire.initialize()
    session_id = await wire.new_session()
    wire.send(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "wait"}],
            },
        }
    )
    await asyncio.wait_for(wire.bridge.prompt_started.wait(), timeout=1)
    wire.send(
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        }
    )

    response = await wire.response(9)
    assert response["result"]["stopReason"] == "cancelled"
    assert wire.bridge.cancelled == [session_id]
    messages = wire.writer.messages()
    update_index = next(i for i, item in enumerate(messages) if item.get("method") == "session/update")
    assert update_index < messages.index(response)
    assert all(item.get("method") != "session/cancel" for item in messages)


@pytest.mark.asyncio
async def test_grok_raw_client_string_id_and_escaped_slash_method_are_accepted(wire) -> None:
    await wire.initialize()
    session_id = await wire.new_session()
    wire.send_raw(
        (
            '{"jsonrpc":"2.0","id":"swift-foundation-uuid",'
            '"method":"session\\/prompt","params":'
            f'{{"sessionId":"{session_id}","prompt":'
            '[{"type":"text","text":"raw"}]}}\n'
        ).encode()
    )

    response = await wire.response("swift-foundation-uuid")
    assert response["id"] == "swift-foundation-uuid"
    assert response["result"]["stopReason"] == "end_turn"


@pytest.mark.asyncio
async def test_authenticate_is_required_only_when_initialize_advertises_methods() -> None:
    class AuthBridge(RecordingBridge):
        capabilities = BridgeCapabilities(
            auth_methods=({"id": "local", "name": "Local account"},)
        )

    bridge = AuthBridge()
    wire = await WireHarness(bridge).start()
    try:
        init = await wire.initialize()
        assert init["result"]["authMethods"] == [
            {"id": "local", "name": "Local account"}
        ]
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {"cwd": "/tmp/project", "mcpServers": []},
            }
        )
        assert (await wire.response(1))["error"]["code"] == AUTH_REQUIRED

        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "authenticate",
                "params": {"methodId": "wrong"},
            }
        )
        assert (await wire.response(2))["error"]["code"] == INVALID_PARAMS

        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "authenticate",
                "params": {"methodId": "local"},
            }
        )
        assert (await wire.response(3))["result"] == {}
        assert bridge.authenticated[0].method_id == "local"
        assert await wire.new_session(4) == "session-1"
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_standard_error_codes_and_unknown_extension_behavior(wire) -> None:
    wire.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": "/tmp/project", "mcpServers": []},
        }
    )
    assert (await wire.response(1))["error"]["code"] == INVALID_REQUEST
    await wire.initialize()

    wire.send(
        {"jsonrpc": "2.0", "id": 2, "method": "unknown/method", "params": {}}
    )
    assert (await wire.response(2))["error"]["code"] == METHOD_NOT_FOUND

    wire.send(
        {"jsonrpc": "2.0", "id": 3, "method": "_example.com/missing", "params": {}}
    )
    extension = await wire.response(3)
    assert extension["error"]["code"] == METHOD_NOT_FOUND
    assert extension["error"]["data"]["method"] == "_example.com/missing"

    before = len(wire.writer.messages())
    wire.send(
        {"jsonrpc": "2.0", "method": "unknown/notification", "params": {}}
    )
    wire.send(
        {"jsonrpc": "2.0", "method": "_example.com/notification", "params": {}}
    )
    await asyncio.sleep(0.02)
    assert len(wire.writer.messages()) == before

    wire.send(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "session/prompt",
            "params": {
                "sessionId": "missing",
                "prompt": [{"type": "text", "text": "hi"}],
            },
        }
    )
    assert (await wire.response(4))["error"]["code"] == RESOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_invalid_schema_does_not_echo_secret_input(wire) -> None:
    await wire.initialize()
    wire.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": "relative", "mcpServers": [], "secret": "do-not-echo"},
        }
    )
    response = await wire.response(1)
    assert response["error"]["code"] == INVALID_PARAMS
    assert "do-not-echo" not in json.dumps(response)


@pytest.mark.asyncio
async def test_parse_ndjson_depth_and_message_size_boundaries() -> None:
    bridge = RecordingBridge()
    limits = AcpLimits(max_message_bytes=512, max_json_depth=8)
    wire = await WireHarness(bridge, limits=limits).start()
    try:
        wire.send_raw(b'{"jsonrpc":"2.0",oops}\n')
        first = (await wire.writer.wait_for_count(1))[0]
        assert first["error"]["code"] == PARSE_ERROR

        deep: object = "x"
        for _ in range(12):
            deep = {"x": deep}
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "_meta": deep,
                },
            }
        )
        second = (await wire.writer.wait_for_count(2))[1]
        assert second["error"]["code"] == INVALID_REQUEST
        assert second["error"]["data"]["reason"] == "message_too_deep"

        wire.send_raw(b"{" + b" " * 600 + b"}\n")
        third = (await wire.writer.wait_for_count(3))[2]
        assert third["error"]["code"] == INVALID_REQUEST
        assert third["error"]["data"]["reason"] == "message_too_large"
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_oversized_bridge_update_is_not_written_to_stdout() -> None:
    class OversizedUpdateBridge(RecordingBridge):
        async def prompt(self, request, emit_update):
            await emit_update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "x" * 2_000},
                }
            )
            return {"stopReason": "end_turn"}

    limits = AcpLimits(max_message_bytes=512)
    wire = await WireHarness(OversizedUpdateBridge(), limits=limits).start()
    try:
        await wire.initialize()
        session_id = await wire.new_session()
        wire.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "go"}],
                },
            }
        )
        response = await wire.response(3)
        assert response["error"]["code"] == INTERNAL_ERROR
        assert response["error"]["data"]["reason"] == "outbound_message_too_large"
        assert all(len(line) + 1 <= limits.max_message_bytes for line in bytes(wire.writer.buffer).splitlines())
        assert all(message.get("method") != "session/update" for message in wire.writer.messages())
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_session_count_and_request_concurrency_are_bounded() -> None:
    sessions = await WireHarness(
        RecordingBridge(),
        limits=AcpLimits(max_sessions=1),
    ).start()
    try:
        await sessions.initialize()
        await sessions.new_session(1)
        sessions.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "/tmp/project", "mcpServers": []},
            }
        )
        assert (await sessions.response(2))["error"]["code"] == SESSION_LIMIT_REACHED
    finally:
        await sessions.close()

    class BlockingNewBridge(RecordingBridge):
        def __init__(self) -> None:
            super().__init__()
            self.new_started = asyncio.Event()
            self.new_release = asyncio.Event()

        async def new_session(self, request):
            self.new_started.set()
            await self.new_release.wait()
            return await super().new_session(request)

    bridge = BlockingNewBridge()
    concurrent = await WireHarness(
        bridge,
        limits=AcpLimits(max_concurrent_requests=1),
    ).start()
    try:
        await concurrent.initialize()
        concurrent.send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/new",
                "params": {"cwd": "/tmp/project", "mcpServers": []},
            }
        )
        await asyncio.wait_for(bridge.new_started.wait(), timeout=1)
        concurrent.send(
            {"jsonrpc": "2.0", "id": 6, "method": "_example.com/ping", "params": {}}
        )
        busy = await concurrent.response(6)
        assert busy["error"]["code"] == SERVER_BUSY
        bridge.new_release.set()
        assert (await concurrent.response(5))["result"]["sessionId"] == "session-1"
    finally:
        await concurrent.close()


@pytest.mark.asyncio
async def test_eof_cancels_active_prompt_and_detaches_connection_sessions() -> None:
    bridge = RecordingBridge()
    bridge.block_prompt = True
    wire = await WireHarness(bridge).start()
    await wire.initialize()
    session_id = await wire.new_session()
    wire.send(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "wait"}],
            },
        }
    )
    await asyncio.wait_for(bridge.prompt_started.wait(), timeout=1)
    await wire.close()
    assert bridge.cancelled == [session_id]
    assert bridge.disconnected == (session_id,)
