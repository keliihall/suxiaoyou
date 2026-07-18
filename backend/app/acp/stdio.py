"""Actual stdin/stdout transport for the bounded ACP server."""

from __future__ import annotations

from acp.stdio import stdio_streams

from app.acp.bridge import SessionPromptBridge
from app.acp.server import AcpLimits, AcpServer


async def run_stdio(
    bridge: SessionPromptBridge,
    *,
    limits: AcpLimits | None = None,
    enabled: bool | None = None,
) -> None:
    """Run ACP on process stdio.

    The official SDK supplies the cross-platform stream adapters. Only compact
    newline-delimited JSON-RPC is written to stdout; application logging must
    use the standard logging package (stderr in the packaged launcher).
    """

    effective_limits = limits or AcpLimits()
    server = AcpServer(bridge, limits=effective_limits, enabled=enabled)
    if not server.enabled:
        # Fail before connecting either process pipe. This is a code-owned
        # release boundary, not a runtime environment opt-in.
        from app.acp.server import AcpFeatureDisabled

        raise AcpFeatureDisabled("ACP stdio is disabled by the v1.1 release gate")
    # The extra byte permits a complete maximum-size frame including newline;
    # larger unterminated input trips StreamReader's bounded-line error.
    reader, writer = await stdio_streams(
        limit=effective_limits.max_message_bytes + 1
    )
    await server.serve(reader, writer)


__all__ = ["run_stdio"]
