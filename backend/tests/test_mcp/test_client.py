"""Tests for MCP client wrapper."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from unittest.mock import AsyncMock, MagicMock, patch

from app.mcp.client import McpClient, sanitise_name
from app.mcp.local_approval import (
    LocalMcpApprovalRequired,
    local_mcp_launch_spec,
)


def _approved_client(name: str, config: dict) -> McpClient:
    return McpClient(
        name,
        config,
        approved_local_fingerprint=local_mcp_launch_spec(config).fingerprint,
    )


class TestSanitiseName:
    def test_clean(self):
        assert sanitise_name("my_tool") == "my_tool"

    def test_special_chars(self):
        assert sanitise_name("my tool@v2!") == "my_tool_v2_"

    def test_hyphens(self):
        assert sanitise_name("my-tool") == "my-tool"


class TestToolId:
    def test_format(self):
        client = McpClient("server-1", {"type": "local"})
        assert client.tool_id("read_file") == "server-1_read_file"

    def test_sanitised(self):
        client = McpClient("my server", {"type": "local"})
        assert client.tool_id("my tool") == "my_server_my_tool"


class TestClientProperties:
    def test_server_type_local(self):
        c = McpClient("test", {"type": "local"})
        assert c.server_type == "local"

    def test_server_type_remote(self):
        c = McpClient("test", {"type": "remote", "url": "http://x"})
        assert c.server_type == "remote"

    def test_default_type(self):
        c = McpClient("test", {})
        assert c.server_type == "local"

    def test_default_timeout(self):
        c = McpClient("test", {})
        assert c.timeout == 30

    def test_custom_timeout(self):
        c = McpClient("test", {"timeout": 60})
        assert c.timeout == 60

    def test_connector_provenance_defaults_custom_and_is_snapshotted(self):
        config = {"connector_provenance": "builtin"}
        c = McpClient("test", config)
        config["connector_provenance"] = "custom"
        assert c.connector_provenance == "builtin"

        assert McpClient("missing", {}).connector_provenance == "custom"
        assert McpClient(
            "malformed",
            {"connector_provenance": ["builtin"]},
        ).connector_provenance == "custom"


class TestOAuthToken:
    def test_set_and_clear(self):
        c = McpClient("test", {})
        assert c._oauth_token is None
        c.set_oauth_token("tok123")
        assert c._oauth_token == "tok123"
        c.set_oauth_token(None)
        assert c._oauth_token is None


class TestListTools:
    def test_empty(self):
        c = McpClient("test", {})
        assert c.list_tools() == []

    def test_returns_copy(self):
        c = McpClient("test", {})
        c._tools = [MagicMock(), MagicMock()]
        tools = c.list_tools()
        assert len(tools) == 2
        assert tools is not c._tools


class TestCallTool:
    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        c = McpClient("test", {})
        with pytest.raises(RuntimeError, match="not connected"):
            await c.call_tool("read", {})

    @pytest.mark.asyncio
    async def test_delegates_to_session(self):
        c = McpClient("test", {})
        c._session = MagicMock()
        c._session.call_tool = AsyncMock(return_value="result")
        result = await c.call_tool("read", {"path": "/tmp"})
        c._session.call_tool.assert_awaited_once_with("read", {"path": "/tmp"})


class TestClose:
    @pytest.mark.asyncio
    async def test_resets_state(self):
        c = McpClient("test", {})
        c.status = "connected"
        c._tools = [MagicMock()]
        c._exit_stack = None
        await c.close()
        assert c.status == "disconnected"
        assert c._tools == []


class TestConnectStdio:
    @pytest.mark.asyncio
    async def test_unapproved_launch_never_reaches_stdio_transport(
        self,
        private_test_executable: str,
    ):
        c = McpClient(
            "test",
            {"type": "local", "command": [private_test_executable]},
        )

        with patch("app.mcp.client.stdio_client") as spawn:
            await c.connect()

        assert c.status == "needs_approval"
        assert c.error is None
        assert c._exit_stack is None
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_stdio_call_is_also_fail_closed(
        self,
        private_test_executable: str,
    ):
        c = McpClient(
            "test",
            {"type": "local", "command": [private_test_executable]},
        )
        c._exit_stack = MagicMock()

        with (
            patch("app.mcp.client.stdio_client") as spawn,
            pytest.raises(LocalMcpApprovalRequired),
        ):
            await c._connect_stdio()

        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_approved_exact_launch_reaches_transport_with_snapshot(
        self,
        private_test_executable: str,
    ):
        config = {
            "type": "local",
            "command": [private_test_executable, "--safe"],
            "environment": {"MODE": "reviewed"},
        }
        launch = local_mcp_launch_spec(config)
        c = _approved_client("test", config)
        c._exit_stack = MagicMock()
        session = MagicMock()
        session.initialize = AsyncMock()
        c._exit_stack.enter_async_context = AsyncMock(
            side_effect=[("read", "write"), session]
        )

        with (
            patch("app.mcp.client.stdio_client", return_value="transport") as spawn,
            patch("app.mcp.client.ClientSession", return_value=session),
        ):
            await c._connect_stdio()

        params = spawn.call_args.args[0]
        assert params.command == private_test_executable
        assert params.args == ["--safe"]
        assert params.env == launch.environment
        assert str(params.cwd) == launch.cwd
        session.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_change_invalidates_prior_approval_before_spawn(
        self,
        private_test_executable: str,
    ):
        config = {
            "type": "local",
            "command": [private_test_executable, "--read"],
        }
        c = _approved_client("test", config)
        config["command"] = [private_test_executable, "--write"]

        with patch("app.mcp.client.stdio_client") as spawn:
            await c.connect()

        assert c.status == "needs_approval"
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_command_raises(self):
        c = McpClient("test", {"type": "local", "command": []})
        c._exit_stack = MagicMock()
        c._exit_stack.__aenter__ = AsyncMock()
        with pytest.raises(ValueError, match="command"):
            await c._connect_stdio()


class TestConnectTimeout:
    @pytest.mark.asyncio
    async def test_configured_timeout_bounds_connection(
        self,
        private_test_executable: str,
    ):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def never_connects():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        c = _approved_client(
            "slow",
            {
                "type": "local",
                "command": [private_test_executable],
                "timeout": 0.01,
            },
        )
        c._connect_stdio = AsyncMock(side_effect=never_connects)  # type: ignore[method-assign]

        await c.connect()

        assert started.is_set()
        assert cancelled.is_set()
        assert c.status == "failed"
        assert c.error == "Connection timed out after 0.1s"
        assert c._exit_stack is None

    @pytest.mark.asyncio
    async def test_caller_cancellation_cleans_up_and_propagates(
        self,
        private_test_executable: str,
    ):
        started = asyncio.Event()

        async def never_connects():
            started.set()
            await asyncio.Event().wait()

        c = _approved_client(
            "slow",
            {
                "type": "local",
                "command": [private_test_executable],
                "timeout": 60,
            },
        )
        c._connect_stdio = AsyncMock(side_effect=never_connects)  # type: ignore[method-assign]

        task = asyncio.create_task(c.connect())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert c._exit_stack is None


class TestConnectRemote:
    @pytest.mark.asyncio
    async def test_remote_transport_does_not_require_local_approval(self):
        c = McpClient("remote", {"type": "remote", "url": "https://example.test/mcp"})
        session = MagicMock()
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

        async def connect_remote() -> None:
            c._session = session

        c._connect_remote = AsyncMock(side_effect=connect_remote)  # type: ignore[method-assign]

        await c.connect()

        assert c.status == "connected"
        c._connect_remote.assert_awaited_once()
