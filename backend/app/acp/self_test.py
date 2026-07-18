"""Authority-free ACP stdio probe for the frozen desktop bundle.

This module deliberately does not import the application, providers, tools,
credentials, storage, or workspace services.  Its explicit launcher command
may bypass the closed release gate only because the bridge can negotiate one
fixed synthetic session and wait for cancellation; it cannot execute user or
model work.  The production ``--acp-stdio`` path remains code-gated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.acp.bridge import BridgeCapabilities, SessionPromptBridge, UpdateEmitter


BUNDLE_SMOKE_SESSION_ID = "bundle-smoke-session"


class BundleSmokeBridge(SessionPromptBridge):
    """Minimal bridge that proves framing, updates, and cancellation only."""

    capabilities = BridgeCapabilities()

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()

    async def new_session(self, request: Any) -> dict[str, str]:
        del request
        return {"sessionId": BUNDLE_SMOKE_SESSION_ID}

    async def prompt(
        self,
        request: Any,
        emit_update: UpdateEmitter,
    ) -> dict[str, str]:
        if request.session_id != BUNDLE_SMOKE_SESSION_ID:
            raise RuntimeError("ACP bundle smoke received an unknown session")
        await emit_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "bundle-smoke-message",
                "content": {"type": "text", "text": "bundle-smoke-ready"},
            }
        )
        await self._cancelled.wait()
        return {"stopReason": "cancelled"}

    async def cancel(self, session_id: str) -> None:
        if session_id == BUNDLE_SMOKE_SESSION_ID:
            self._cancelled.set()


async def run_bundle_smoke_stdio(
    *,
    stdio_runner: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Serve the synthetic bridge through the SDK's real stdio adapter."""

    if stdio_runner is None:
        from app.acp.stdio import run_stdio

        stdio_runner = run_stdio
    await stdio_runner(BundleSmokeBridge(), enabled=True)


def main() -> int:
    """Frozen executable entry point used only by the bundle verifier."""

    asyncio.run(run_bundle_smoke_stdio())
    return 0


__all__ = [
    "BUNDLE_SMOKE_SESSION_ID",
    "BundleSmokeBridge",
    "main",
    "run_bundle_smoke_stdio",
]
