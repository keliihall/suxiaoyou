from __future__ import annotations

import asyncio
import json

import pytest

from app import release_features
from app.acp import AcpFeatureDisabled, AcpServer

from .conftest import CaptureWriter, RecordingBridge, WireHarness


@pytest.mark.asyncio
async def test_code_owned_gate_can_be_closed_without_consuming_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert release_features.V11_ACP_RELEASED is True
    monkeypatch.setattr(release_features, "V11_ACP_RELEASED", False)
    bridge = RecordingBridge()
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":0,"method":"initialize",'
        b'"params":{"protocolVersion":1}}\n'
    )
    writer = CaptureWriter()

    with pytest.raises(AcpFeatureDisabled):
        await AcpServer(bridge).serve(reader, writer)

    assert bytes(writer.buffer) == b""
    assert bridge.initialized == []


@pytest.mark.asyncio
async def test_stdout_contract_is_compact_ndjson_without_pollution(capsys) -> None:
    wire = await WireHarness(RecordingBridge()).start()
    try:
        await wire.initialize("uuid/with/slash")
        await wire.new_session("new-uuid")
        raw = bytes(wire.writer.buffer)
        assert raw.endswith(b"\n")
        assert b"\n\n" not in raw
        assert b": " not in raw
        for line in raw.splitlines():
            parsed = json.loads(line)
            assert parsed["jsonrpc"] == "2.0"
            assert isinstance(parsed, dict)
    finally:
        await wire.close()

    captured = capsys.readouterr()
    assert captured.out == ""


@pytest.mark.asyncio
async def test_incomplete_frame_and_duplicate_keys_are_rejected() -> None:
    wire = await WireHarness(RecordingBridge()).start()
    try:
        wire.send_raw(
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"initialize",'
            b'"params":{"protocolVersion":1}}\n'
        )
        duplicate = (await wire.writer.wait_for_count(1))[0]
        assert duplicate["error"]["code"] == -32700
        wire.send_raw(b'{"jsonrpc":"2.0","id":3}')
        # StreamReader can identify an unterminated final NDJSON frame only
        # when stdin reaches EOF.
        wire.reader.feed_eof()
        incomplete = (await wire.writer.wait_for_count(2))[1]
        assert incomplete["error"]["code"] == -32600
        assert incomplete["error"]["data"]["reason"] == "incomplete_ndjson_frame"
    finally:
        await wire.close()
