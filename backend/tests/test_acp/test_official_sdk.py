from __future__ import annotations

import asyncio

from acp.client.connection import ClientSideConnection
from acp.schema import (
    ClientCapabilities,
    Implementation,
    RequestPermissionRequest,
    RequestPermissionResponse,
    TextContentBlock,
)
import pytest

from app.acp import AcpServer

from .conftest import RecordingBridge


class OfficialSdkClient:
    def __init__(self) -> None:
        self.updates = []
        self.permission_requests = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))

    async def request_permission(
        self,
        options,
        session_id,
        tool_call,
        **kwargs,
    ):
        self.permission_requests.append((session_id, options, tool_call))
        allow = next(option for option in options if option.kind == "allow_once")
        return RequestPermissionResponse.model_validate(
            {"outcome": {"outcome": "selected", "optionId": allow.option_id}}
        )


@pytest.mark.asyncio
async def test_official_python_010_client_round_trip() -> None:
    """Prove interoperability through the SDK's own connection and models."""

    bridge = RecordingBridge()
    server_done = asyncio.Event()

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await AcpServer(bridge, enabled=True).serve(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()
            server_done.set()

    listener = await asyncio.start_server(accept, "127.0.0.1", 0)
    address = listener.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(address[0], address[1])
    client = OfficialSdkClient()
    connection = ClientSideConnection(client, writer, reader)
    try:
        initialized = await connection.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="official-test", version="0.10.1"),
        )
        assert initialized.protocol_version == 1
        assert initialized.agent_capabilities.load_session is True

        created = await connection.new_session(
            cwd="/tmp/project",
            mcp_servers=[],
        )
        prompted = await connection.prompt(
            session_id=created.session_id,
            prompt=[TextContentBlock(type="text", text="official")],
        )
        assert prompted.stop_reason == "end_turn"

        await asyncio.wait_for(
            _wait_for_updates(client, 1),
            timeout=1,
        )
        assert client.updates[0][0] == created.session_id
        assert client.updates[0][1].session_update == "agent_message_chunk"
    finally:
        await connection.close()
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()
        await asyncio.wait_for(server_done.wait(), timeout=1)


@pytest.mark.asyncio
async def test_official_python_client_handles_reverse_permission_request() -> None:
    class ReverseSdkBridge(RecordingBridge):
        def __init__(self) -> None:
            super().__init__()
            self.requester = None
            self.response = None

        def bind_permission_requester(self, requester) -> None:
            self.requester = requester

        async def prompt(self, request, emit_update):
            del emit_update
            assert self.requester is not None
            permission = RequestPermissionRequest.model_validate(
                {
                    "sessionId": request.session_id,
                    "options": [
                        {
                            "optionId": "private-allow",
                            "kind": "allow_once",
                            "name": "private allow name",
                        },
                        {
                            "optionId": "private-reject",
                            "kind": "reject_once",
                            "name": "private reject name",
                        },
                    ],
                    "toolCall": {
                        "toolCallId": "private-call-id",
                        "title": "private command and path",
                    },
                }
            )
            self.response = await self.requester(permission)
            return {"stopReason": "end_turn"}

    bridge = ReverseSdkBridge()
    server_done = asyncio.Event()

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await AcpServer(bridge, enabled=True).serve(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()
            server_done.set()

    listener = await asyncio.start_server(accept, "127.0.0.1", 0)
    address = listener.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(address[0], address[1])
    client = OfficialSdkClient()
    connection = ClientSideConnection(client, writer, reader)
    try:
        await connection.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="official-reverse-test", version="0.10.1"),
        )
        created = await connection.new_session(cwd="/tmp/project", mcp_servers=[])
        prompted = await connection.prompt(
            session_id=created.session_id,
            prompt=[TextContentBlock(type="text", text="official reverse")],
        )

        assert prompted.stop_reason == "end_turn"
        assert bridge.response.outcome.outcome == "selected"
        assert bridge.response.outcome.option_id == "private-allow"
        assert len(client.permission_requests) == 1
        session_id, options, tool_call = client.permission_requests[0]
        assert session_id == created.session_id
        assert [option.option_id for option in options] == ["option-1", "option-2"]
        assert tool_call.title == "Permission required"
        assert tool_call.locations is None
        assert tool_call.raw_input is None
        assert tool_call.raw_output is None
    finally:
        await connection.close()
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()
        await asyncio.wait_for(server_done.wait(), timeout=1)


async def _wait_for_updates(client: OfficialSdkClient, count: int) -> None:
    while len(client.updates) < count:
        await asyncio.sleep(0)
