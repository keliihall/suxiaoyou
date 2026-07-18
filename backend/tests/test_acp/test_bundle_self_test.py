from __future__ import annotations

from app.acp.self_test import BUNDLE_SMOKE_SESSION_ID, BundleSmokeBridge

from .conftest import WireHarness


async def test_bundle_smoke_bridge_negotiates_and_cancels_without_authority() -> None:
    bridge = BundleSmokeBridge()
    wire = await WireHarness(bridge, enabled=True).start()
    try:
        initialized = await wire.initialize("bundle-init")
        assert initialized["result"]["protocolVersion"] == 1
        session_id = await wire.new_session("bundle-new")
        assert session_id == BUNDLE_SMOKE_SESSION_ID

        wire.send(
            {
                "jsonrpc": "2.0",
                "id": "bundle-prompt",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "cancel me"}],
                },
            }
        )
        messages = await wire.writer.wait_for_count(3)
        assert any(item.get("method") == "session/update" for item in messages)
        wire.send(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
            }
        )

        response = await wire.response("bundle-prompt")
        assert response["result"] == {"stopReason": "cancelled"}
        assert bridge._cancelled.is_set()
    finally:
        await wire.close()


async def test_bundle_smoke_stdio_explicitly_uses_authority_free_override() -> None:
    from app.acp.self_test import run_bundle_smoke_stdio

    observed: list[tuple[object, bool]] = []

    async def runner(bridge, *, enabled):
        observed.append((bridge, enabled))

    await run_bundle_smoke_stdio(stdio_runner=runner)

    assert len(observed) == 1
    assert isinstance(observed[0][0], BundleSmokeBridge)
    assert observed[0][1] is True
    assert not observed[0][0]._cancelled.is_set()
