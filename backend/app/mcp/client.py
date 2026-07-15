"""MCP client — connects to a single MCP server and proxies tool calls."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from contextlib import AsyncExitStack
from typing import Any, Callable

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent, Tool as McpTool

from app.connector.model import (
    CONNECTOR_PROVENANCE_CUSTOM,
    REMOTE_AUTH_OAUTH_BEARER,
    REMOTE_AUTH_RAW_AUTHORIZATION,
    SUPPORTED_CONNECTOR_PROVENANCE,
    SUPPORTED_REMOTE_AUTH_MODES,
)
from app.mcp.local_approval import (
    LocalMcpApprovalRequired,
    LocalMcpLaunchSpec,
    local_mcp_launch_spec,
)

logger = logging.getLogger(__name__)

# Sanitise names to only contain alphanumeric, underscore, hyphen
_SANITISE_RE = re.compile(r"[^a-zA-Z0-9_-]")


class ExternalRuntimeStartBlocked(RuntimeError):
    """Raised when the global runtime barrier closes during connection."""


def sanitise_name(name: str) -> str:
    return _SANITISE_RE.sub("_", name)


class McpClient:
    """Wrapper around a single MCP server connection."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        *,
        approved_local_fingerprint: str | None = None,
        local_approval_check: Callable[[str], bool] | None = None,
        start_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self.name = name
        self.config = config
        configured_provenance = config.get(
            "connector_provenance",
            CONNECTOR_PROVENANCE_CUSTOM,
        )
        # Snapshot provenance at construction.  Missing/malformed direct MCP
        # configs are custom, and later mutation of the config dict cannot
        # promote a live client to built-in trust.
        self._connector_provenance = (
            configured_provenance
            if isinstance(configured_provenance, str)
            and configured_provenance in SUPPORTED_CONNECTOR_PROVENANCE
            else CONNECTOR_PROVENANCE_CUSTOM
        )
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._tools: list[McpTool] = []
        # connected | disconnected | failed | needs_auth | needs_approval
        self.status: str = "disconnected"
        self.error: str | None = None
        self._oauth_token: str | None = None  # injected OAuth access token
        # This value is supplied by McpManager's app-private approval store,
        # never read from connector/plugin configuration.  A plugin therefore
        # cannot mark its own process command as trusted.
        self._approved_local_fingerprint = approved_local_fingerprint
        self._local_approval_check = local_approval_check
        self._start_allowed = start_allowed or (lambda: True)

    @property
    def server_type(self) -> str:
        return self.config.get("type", "local")

    @property
    def connector_provenance(self) -> str:
        return self._connector_provenance

    @property
    def timeout(self) -> int | float:
        return self.config.get("timeout", 30)

    @property
    def auth_mode(self) -> str:
        mode = self.config.get("auth_mode", REMOTE_AUTH_OAUTH_BEARER)
        if mode not in SUPPORTED_REMOTE_AUTH_MODES:
            raise ValueError(f"Unsupported MCP authentication mode: {mode!r}")
        return mode

    async def connect(self) -> None:
        """Connect to the MCP server and discover tools."""
        if not self._start_allowed():
            self.status = "disconnected"
            self.error = None
            return
        try:
            timeout_seconds = max(0.1, float(self.timeout))
        except (TypeError, ValueError):
            timeout_seconds = 30.0
        try:
            async with asyncio.timeout(timeout_seconds):
                if self.server_type == "local":
                    # Check before even allocating a transport exit stack.  The
                    # stdio method checks again and consumes a validated
                    # snapshot, so direct callers and concurrent config drift
                    # cannot reach spawn through this path.
                    self._approved_local_launch_spec()
                    self._exit_stack = AsyncExitStack()
                    await self._exit_stack.__aenter__()
                    await self._connect_stdio()
                else:
                    await self._connect_remote()

                # Manager/registry transition locks are the primary barrier.
                # This second check also protects direct McpClient callers and
                # closes a guard flip that happens while a transport connects.
                if not self._start_allowed():
                    raise ExternalRuntimeStartBlocked(
                        "External runtime stopped during MCP connection"
                    )

                # Discover tools
                result = await self._session.list_tools()  # type: ignore[union-attr]
                discovered = list(result.tools)
                self._tools = [
                    tool for tool in discovered if self.is_tool_allowed(tool.name)
                ]
                filtered_count = len(discovered) - len(self._tools)
                if filtered_count:
                    logger.warning(
                        "MCP server '%s': filtered %d tool(s) outside its allowlist",
                        self.name,
                        filtered_count,
                    )
                self.status = "connected"
                self.error = None
                logger.info(
                    "MCP server '%s' connected — %d tools available",
                    self.name,
                    len(self._tools),
                )
        except asyncio.CancelledError:
            await self._cleanup()
            raise
        except LocalMcpApprovalRequired:
            self.status = "needs_approval"
            self.error = None
            await self._cleanup()
        except ExternalRuntimeStartBlocked:
            self.status = "disconnected"
            self.error = None
            await self._cleanup()
        except Exception as e:
            self.status = "failed"
            raw_error = (
                f"Connection timed out after {timeout_seconds:g}s"
                if isinstance(e, TimeoutError)
                else str(e)
            )
            self.error = self._redact_token(raw_error)
            logger.warning(
                "Failed to connect to MCP server '%s': %s",
                self.name,
                self.error,
            )
            await self._cleanup()

    async def _connect_stdio(self) -> None:
        """Connect via stdio transport (local subprocess)."""
        launch = self._approved_local_launch_spec()
        if not self._start_allowed():
            raise ExternalRuntimeStartBlocked(
                "External runtime stopped before local MCP spawn"
            )
        server_params = StdioServerParameters(
            command=launch.command[0],
            args=list(launch.command[1:]),
            # Pass the exact effective environment snapshot reviewed by the
            # fingerprint.  The SDK's default environment is already included.
            env=dict(launch.environment),
            cwd=launch.cwd,
        )

        read_stream, write_stream = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            stdio_client(server_params)
        )
        self._session = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

    def local_startup_approval(self) -> dict[str, Any] | None:
        """Describe the current local launch without exposing env values."""

        if self.server_type != "local":
            return None
        launch = local_mcp_launch_spec(self.config)
        descriptor = launch.public_descriptor()
        descriptor["approved"] = (
            self._approved_local_fingerprint == launch.fingerprint
        )
        descriptor["required"] = not descriptor["approved"]
        return descriptor

    def _approved_local_launch_spec(self) -> LocalMcpLaunchSpec:
        launch = local_mcp_launch_spec(self.config)
        if (
            self._approved_local_fingerprint != launch.fingerprint
            or (
                self._local_approval_check is not None
                and not self._local_approval_check(launch.fingerprint)
            )
        ):
            raise LocalMcpApprovalRequired(
                f"MCP server '{self.name}' requires approval for its local startup command"
            )
        return launch

    async def _connect_remote(self) -> None:
        """Connect via HTTP/SSE transport (remote server)."""
        if not self._start_allowed():
            raise ExternalRuntimeStartBlocked(
                "External runtime stopped before remote MCP connection"
            )
        url = self.config.get("url")
        if not url:
            raise ValueError(f"MCP server '{self.name}': 'url' is required for remote type")

        headers = self._request_headers()

        # Try streamable HTTP first, fall back to SSE
        try:
            await self._try_transport(
                lambda stack: self._enter_streamable_http(stack, url, headers),
            )
            return
        except Exception as e:
            logger.warning(
                "MCP server '%s': streamable HTTP failed: %s",
                self.name,
                self._redact_token(str(e)),
            )

        # Fall back to SSE
        try:
            await self._try_transport(
                lambda stack: self._enter_sse(stack, url, headers),
            )
        except Exception as e:
            logger.warning(
                "MCP server '%s': SSE also failed: %s",
                self.name,
                self._redact_token(str(e)),
            )
            raise

    async def _try_transport(self, enter_fn: Any) -> None:
        """Attempt a transport connection with its own isolated exit stack.

        On success, promotes the stack to self._exit_stack.
        On failure, cleans up the stack defensively and re-raises.

        NOTE: We must NOT wrap the context-manager entry in
        ``anyio.fail_after`` because the MCP SDK's transports create
        internal task-groups / cancel-scopes that must be entered and
        exited in the same task context.  ``fail_after`` creates a
        cancel scope that conflicts with this requirement.
        """
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await enter_fn(stack)
        except asyncio.CancelledError:
            await self._close_stack(stack)
            raise
        except BaseException as e:
            await self._close_stack(stack)
            if isinstance(e, Exception):
                raise
            raise RuntimeError(f"MCP transport failed: {e}") from e
        # Success — adopt the stack
        self._exit_stack = stack

    async def _enter_streamable_http(
        self, stack: AsyncExitStack, url: str, headers: dict[str, str],
    ) -> None:
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamablehttp_client(url, headers=headers)
        )
        self._session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

    async def _enter_sse(
        self, stack: AsyncExitStack, url: str, headers: dict[str, str],
    ) -> None:
        read_stream, write_stream = await stack.enter_async_context(
            sse_client(url, headers=headers)
        )
        self._session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

    async def close(self) -> None:
        """Disconnect from the MCP server."""
        await self._cleanup()
        self.status = "disconnected"
        self._tools = []

    @staticmethod
    async def _close_stack(stack: AsyncExitStack) -> None:
        """Close an exit stack, swallowing errors from cross-task cancel scope teardown."""
        try:
            await stack.aclose()
        except BaseException:
            # CancelledError (BaseException in Python 3.9+) and RuntimeError from
            # cross-task cancel scope teardown must both be suppressed here.
            logger.debug("Error closing MCP exit stack (suppressed)", exc_info=True)

    async def _cleanup(self) -> None:
        if self._exit_stack:
            await self._close_stack(self._exit_stack)
            self._exit_stack = None
        self._session = None

    def set_oauth_token(self, token: str | None) -> None:
        """Set or clear the protected remote-auth token for this client.

        The historical method name is retained for compatibility.  How the
        token is serialized on the wire is controlled exclusively by the
        connector's trusted ``auth_mode``.
        """
        self._oauth_token = token

    def _request_headers(self) -> dict[str, str]:
        """Build the only supported remote MCP authentication header.

        Arbitrary header dictionaries are deliberately rejected.  Connector
        credentials must travel through McpTokenStore/CredentialStore and the
        allow-listed auth-mode enum, never through persisted connector JSON.
        """

        configured_headers = self.config.get("headers")
        if configured_headers:
            raise ValueError(
                "Arbitrary remote MCP headers are not supported; use a managed auth mode"
            )
        if not self._oauth_token:
            return {}
        if self.auth_mode == REMOTE_AUTH_RAW_AUTHORIZATION:
            return {"Authorization": self._oauth_token}
        return {"Authorization": f"Bearer {self._oauth_token}"}

    def _redact_token(self, value: str) -> str:
        return self.scrub_sensitive_text(value)

    def scrub_sensitive_text(self, value: str) -> str:
        """Remove the managed credential from untrusted MCP text.

        Remote servers can echo request headers in transport exceptions or
        tool-level error content.  Keep this scrubber on the client so every
        boundary uses the exact credential that was placed on the wire.
        """
        token = self._oauth_token
        if token:
            return value.replace(token, "[redacted]")
        return value

    def is_tool_allowed(self, tool_name: str) -> bool:
        patterns = self.config.get("allowed_tool_patterns", [])
        if not patterns:
            return True
        return any(
            isinstance(pattern, str) and fnmatch.fnmatchcase(tool_name, pattern)
            for pattern in patterns
        )

    def tool_requires_approval(self, tool_name: str) -> bool:
        patterns = self.config.get("approval_required_tool_patterns", [])
        return any(
            isinstance(pattern, str) and fnmatch.fnmatchcase(tool_name, pattern)
            for pattern in patterns
        )

    def list_tools(self) -> list[McpTool]:
        """Return the list of tools discovered from this server."""
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Call a tool on the MCP server."""
        if not self.is_tool_allowed(tool_name):
            raise PermissionError(
                f"MCP tool '{tool_name}' is outside the connector allowlist"
            )
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.name}' is not connected")
        try:
            return await self._session.call_tool(tool_name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never let an upstream transport/tool exception carry the
            # Authorization token into ToolResult or model context.
            message = self.scrub_sensitive_text(str(exc))
            raise RuntimeError(message or "Remote MCP tool call failed") from None

    def tool_id(self, tool_name: str) -> str:
        """Generate a unique tool ID for a tool from this server."""
        return f"{sanitise_name(self.name)}_{sanitise_name(tool_name)}"
