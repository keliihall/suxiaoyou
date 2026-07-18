from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.acp import (
    AcpLimits,
    AcpServer,
    BridgeCapabilities,
    SessionPromptBridge,
)


class CaptureWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.changed = asyncio.Event()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)
        self.changed.set()

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def messages(self) -> list[dict[str, Any]]:
        lines = bytes(self.buffer).splitlines()
        return [json.loads(line) for line in lines]

    async def wait_for_count(
        self,
        count: int,
        *,
        timeout: float = 2.0,
    ) -> list[dict[str, Any]]:
        async def wait() -> list[dict[str, Any]]:
            while True:
                messages = self.messages()
                if len(messages) >= count:
                    return messages
                self.changed.clear()
                await self.changed.wait()

        return await asyncio.wait_for(wait(), timeout=timeout)


class RecordingBridge(SessionPromptBridge):
    capabilities = BridgeCapabilities(load_session=True)

    def __init__(self) -> None:
        self.initialized = []
        self.authenticated = []
        self.new_requests = []
        self.load_requests = []
        self.prompt_requests = []
        self.cancelled: list[str] = []
        self.disconnected: tuple[str, ...] | None = None
        self.next_session = 1
        self.prompt_started = asyncio.Event()
        self.prompt_release = asyncio.Event()
        self.block_prompt = False

    async def initialize(self, request):
        self.initialized.append(request)

    async def authenticate(self, request):
        self.authenticated.append(request)
        return {}

    async def new_session(self, request):
        self.new_requests.append(request)
        session_id = f"session-{self.next_session}"
        self.next_session += 1
        return {"sessionId": session_id}

    async def load_session(self, request, emit_update):
        self.load_requests.append(request)
        await emit_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "history-1",
                "content": {"type": "text", "text": "history"},
            }
        )
        # The official 0.10 Python Agent interface permits None for this empty
        # response; the server normalizes it to the ACP wire's `{}` result.
        return None

    async def prompt(self, request, emit_update):
        self.prompt_requests.append(request)
        self.prompt_started.set()
        if self.block_prompt:
            await self.prompt_release.wait()
        await emit_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "answer-1",
                "content": {"type": "text", "text": "hello"},
            }
        )
        return {"stopReason": "end_turn"}

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)
        self.prompt_release.set()

    async def disconnect(self, session_ids):
        self.disconnected = tuple(session_ids)


class WireHarness:
    def __init__(
        self,
        bridge: SessionPromptBridge,
        *,
        limits: AcpLimits | None = None,
        enabled: bool = True,
    ) -> None:
        self.bridge = bridge
        self.reader = asyncio.StreamReader(
            limit=(limits or AcpLimits()).max_message_bytes + 1
        )
        self.writer = CaptureWriter()
        self.server = AcpServer(
            bridge,
            limits=limits,
            enabled=enabled,
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> "WireHarness":
        self.task = asyncio.create_task(self.server.serve(self.reader, self.writer))
        await asyncio.sleep(0)
        return self

    def send(self, message: dict[str, Any]) -> None:
        self.send_raw(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def send_raw(self, frame: bytes) -> None:
        self.reader.feed_data(frame)

    async def response(self, request_id: Any) -> dict[str, Any]:
        async def wait() -> dict[str, Any]:
            seen = 0
            while True:
                messages = self.writer.messages()
                for message in messages[seen:]:
                    if "method" not in message and message.get("id") == request_id:
                        return message
                seen = len(messages)
                self.writer.changed.clear()
                await self.writer.changed.wait()

        return await asyncio.wait_for(wait(), timeout=2.0)

    async def close(self) -> None:
        if self.task is None:
            return
        self.reader.feed_eof()
        await asyncio.wait_for(self.task, timeout=2.0)

    async def initialize(self, request_id: Any = 0) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "wire-test", "version": "1"},
                },
            }
        )
        return await self.response(request_id)

    async def new_session(
        self,
        request_id: Any = 1,
        *,
        cwd: str = "/tmp/project",
    ) -> str:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/new",
                "params": {"cwd": cwd, "mcpServers": []},
            }
        )
        response = await self.response(request_id)
        return response["result"]["sessionId"]


@pytest.fixture
async def wire():
    bridge = RecordingBridge()
    harness = await WireHarness(bridge).start()
    try:
        yield harness
    finally:
        await harness.close()
