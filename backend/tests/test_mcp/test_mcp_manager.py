"""Tests for MCP manager lifecycle."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from unittest.mock import AsyncMock, MagicMock, patch

from app.mcp.manager import McpManager
from app.mcp.local_approval import LocalMcpApprovalStore, local_mcp_launch_spec


def _mock_client(name: str, status: str = "connected", tools: list | None = None):
    c = MagicMock()
    c.name = name
    c.status = status
    c.error = None
    c.server_type = "remote"
    c._oauth_token = None
    c.connect = AsyncMock()
    c.close = AsyncMock()
    c.set_oauth_token = MagicMock()
    c.list_tools = MagicMock(return_value=tools or [])
    return c


class TestStartup:
    @pytest.mark.asyncio
    async def test_connects_enabled(self):
        mgr = McpManager({"srv1": {"enabled": True, "url": "http://x"}})
        with patch("app.mcp.manager.McpClient") as MockClient:
            mc = _mock_client("srv1")
            MockClient.return_value = mc
            mgr._token_store = MagicMock(get=MagicMock(return_value=None))
            await mgr.startup()
        assert "srv1" in mgr._clients
        mc.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_disabled(self):
        mgr = McpManager({"srv1": {"enabled": False}})
        mgr._token_store = MagicMock()
        await mgr.startup()
        assert "srv1" not in mgr._clients

    @pytest.mark.asyncio
    async def test_enabled_local_server_waits_for_connection_approval(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        mgr = McpManager(
            {
                "local": {
                    "type": "local",
                    "enabled": True,
                    "command": [private_test_executable, "--startup-side-effect"],
                }
            },
            project_dir=str(tmp_path / "workspace"),
            approval_store=store,
        )
        mgr._token_store = MagicMock(get=MagicMock(return_value=None))

        with patch("app.mcp.client.stdio_client") as spawn:
            await mgr.startup()

        assert mgr.status()["local"]["status"] == "needs_approval"
        assert mgr.status()["local"]["local_approval"]["required"] is True
        spawn.assert_not_called()


class TestShutdown:
    @pytest.mark.asyncio
    async def test_closes_all(self):
        mgr = McpManager({})
        c1 = _mock_client("a")
        c2 = _mock_client("b")
        mgr._clients = {"a": c1, "b": c2}
        await mgr.shutdown()
        c1.close.assert_awaited_once()
        c2.close.assert_awaited_once()
        assert mgr._clients == {}

    @pytest.mark.asyncio
    async def test_startup_cannot_reopen_gate_during_shutdown(self):
        mgr = McpManager({"srv": {"enabled": True, "url": "https://example.test"}})
        existing = _mock_client("srv")
        close_started = asyncio.Event()
        release_close = asyncio.Event()

        async def slow_close() -> None:
            close_started.set()
            await release_close.wait()

        existing.close = AsyncMock(side_effect=slow_close)
        mgr._clients = {"srv": existing}
        shutdown = asyncio.create_task(mgr.shutdown())
        await asyncio.wait_for(close_started.wait(), timeout=1)

        with patch("app.mcp.manager.McpClient") as client_type:
            await mgr.startup()
            client_type.assert_not_called()

        release_close.set()
        await asyncio.wait_for(shutdown, timeout=1)
        assert mgr._clients == {}


class TestTools:
    def test_only_connected(self):
        mgr = McpManager({})
        c1 = _mock_client("a", "connected", [MagicMock()])
        c2 = _mock_client("b", "failed")
        mgr._clients = {"a": c1, "b": c2}
        with patch("app.mcp.manager.McpToolWrapper"):
            tools = mgr.tools()
        assert len(tools) == 1

    def test_empty(self):
        mgr = McpManager({})
        assert mgr.tools() == []


class TestStatus:
    def test_reports_all(self):
        mgr = McpManager({})
        mgr._clients = {
            "a": _mock_client("a", "connected"),
            "b": _mock_client("b", "needs_auth"),
        }
        status = mgr.status()
        assert status["a"]["status"] == "connected"
        assert status["b"]["status"] == "needs_auth"


class TestLocalStartupApproval:
    @pytest.mark.asyncio
    async def test_exact_approval_is_persisted_before_connection(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        config = {
            "type": "local",
            "enabled": True,
            "command": [private_test_executable, "--safe"],
            "environment": {"MODE": "reviewed"},
        }
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        mgr = McpManager(
            {"local": config},
            project_dir=str(tmp_path / "workspace"),
            approval_store=store,
        )
        fingerprint = mgr.local_startup_approval("local")["fingerprint"]

        with patch("app.mcp.manager.McpClient") as client_type:
            client = _mock_client("local", status="disconnected")
            client.server_type = "local"

            async def connect() -> None:
                # The durable store is already committed when spawn is reached.
                assert store.get("local") == fingerprint
                client.status = "connected"

            client.connect = AsyncMock(side_effect=connect)
            client_type.return_value = client
            accepted = await mgr.approve_local_startup("local", fingerprint)
            duplicate = await mgr.approve_local_startup("local", fingerprint)

        assert accepted.approval_persisted is True
        assert accepted.connected is True
        assert duplicate.approval_persisted is True
        assert duplicate.connected is True
        assert duplicate.duplicate is True
        assert store.get("local") == fingerprint
        client_type.assert_called_once()
        call = client_type.call_args
        assert call.args == ("local", config)
        assert call.kwargs["approved_local_fingerprint"] == fingerprint
        assert callable(call.kwargs["local_approval_check"])
        assert call.kwargs["start_allowed"] == mgr._can_start
        client.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_fingerprint_cannot_persist_or_spawn(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        config = {
            "type": "local",
            "enabled": True,
            "command": [private_test_executable, "--read"],
        }
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        mgr = McpManager(
            {"local": config},
            project_dir=str(tmp_path / "workspace"),
            approval_store=store,
        )
        stale = mgr.local_startup_approval("local")["fingerprint"]
        config["command"] = [private_test_executable, "--write"]

        with patch("app.mcp.manager.McpClient") as client_type:
            accepted = await mgr.approve_local_startup("local", stale)

        assert accepted.approval_persisted is False
        assert accepted.connected is False
        assert store.get("local") is None
        client_type.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_does_not_auto_approve_noninteractive_source(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        mgr = McpManager(
            {
                "local": {
                    "type": "local",
                    "enabled": True,
                    "command": [private_test_executable],
                }
            },
            project_dir=str(tmp_path / "workspace"),
            approval_store=store,
        )
        mgr._token_store = MagicMock(get=MagicMock(return_value=None))

        with patch("app.mcp.client.stdio_client") as spawn:
            connected = await mgr.reconnect("local")

        assert connected is False
        assert mgr.status()["local"]["status"] == "needs_approval"
        assert store.get("local") is None
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_concurrent_approval_and_reconnect_spawn_only_once(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        config = {
            "type": "local",
            "enabled": True,
            "command": [private_test_executable, "--one-start"],
        }
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        mgr = McpManager({"local": config}, approval_store=store)
        fingerprint = mgr.local_startup_approval("local")["fingerprint"]
        entered = asyncio.Event()
        release = asyncio.Event()

        with patch("app.mcp.manager.McpClient") as client_type:
            client = _mock_client("local", status="disconnected")
            client.server_type = "local"

            async def connect() -> None:
                entered.set()
                await release.wait()
                client.status = "connected"

            client.connect = AsyncMock(side_effect=connect)
            client_type.return_value = client
            approval_task = asyncio.create_task(
                mgr.approve_local_startup("local", fingerprint)
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            reconnect_task = asyncio.create_task(mgr.reconnect("local"))
            await asyncio.sleep(0)
            release.set()

            approval = await asyncio.wait_for(approval_task, timeout=1)
            reconnected = await asyncio.wait_for(reconnect_task, timeout=1)

        assert approval.connected is True
        assert reconnected is True
        client_type.assert_called_once()
        client.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_closes_gate_while_approval_connect_is_in_flight(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        config = {
            "type": "local",
            "enabled": True,
            "command": [private_test_executable, "--shutdown-race"],
        }
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        mgr = McpManager({"local": config}, approval_store=store)
        fingerprint = mgr.local_startup_approval("local")["fingerprint"]
        entered = asyncio.Event()
        release = asyncio.Event()

        with patch("app.mcp.manager.McpClient") as client_type:
            client = _mock_client("local", status="disconnected")
            client.server_type = "local"

            async def connect() -> None:
                entered.set()
                await release.wait()
                client.status = "connected"

            client.connect = AsyncMock(side_effect=connect)
            client_type.return_value = client
            approval_task = asyncio.create_task(
                mgr.approve_local_startup("local", fingerprint)
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            shutdown_task = asyncio.create_task(mgr.shutdown())
            for _ in range(20):
                if not mgr._accepting_starts:
                    break
                await asyncio.sleep(0)
            assert mgr._accepting_starts is False
            release.set()

            approval = await asyncio.wait_for(approval_task, timeout=1)
            await asyncio.wait_for(shutdown_task, timeout=1)

        assert approval.approval_persisted is True
        assert approval.connected is False
        assert approval.status == "blocked"
        assert mgr._clients == {}
        client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_connection_is_not_reported_as_approval_success(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        config = {
            "type": "local",
            "enabled": True,
            "command": [private_test_executable, "--fails"],
        }
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        mgr = McpManager({"local": config}, approval_store=store)
        fingerprint = mgr.local_startup_approval("local")["fingerprint"]

        with patch("app.mcp.manager.McpClient") as client_type:
            client = _mock_client("local", status="disconnected")
            client.server_type = "local"

            async def fail_connect() -> None:
                client.status = "failed"
                client.error = "connection failed"

            client.connect = AsyncMock(side_effect=fail_connect)
            client_type.return_value = client
            result = await mgr.approve_local_startup("local", fingerprint)

        assert result.approval_persisted is True
        assert result.connected is False
        assert result.status == "failed"
        assert result.error == "connection failed"
        assert store.get("local") == fingerprint

    def test_environment_change_invalidates_persisted_approval(
        self,
        tmp_path,
        private_test_executable: str,
    ) -> None:
        config = {
            "type": "local",
            "command": [private_test_executable],
            "environment": {"TOKEN": "first"},
        }
        store = LocalMcpApprovalStore(
            str(tmp_path / "workspace"),
            storage_root=tmp_path / "approvals",
        )
        old = local_mcp_launch_spec(config).fingerprint
        store.approve("local", old)
        mgr = McpManager(
            {"local": config},
            project_dir=str(tmp_path / "workspace"),
            approval_store=store,
        )
        assert mgr.local_startup_approval("local")["approved"] is True

        config["environment"]["TOKEN"] = "second"

        approval = mgr.local_startup_approval("local")
        assert approval["approved"] is False
        assert approval["required"] is True
        assert approval["fingerprint"] != old


class TestDisconnectAuth:
    @pytest.mark.asyncio
    async def test_clears_and_disconnects(self):
        mgr = McpManager({})
        mgr._token_store = MagicMock()
        c = _mock_client("srv1")
        mgr._clients = {"srv1": c}

        result = await mgr.disconnect_auth("srv1")
        assert result is True
        mgr._token_store.delete.assert_called_once_with("srv1")
        c.set_oauth_token.assert_called_once_with(None)
        c.close.assert_awaited_once()
        assert c.status == "needs_auth"


class TestCompleteAuth:
    @pytest.mark.asyncio
    async def test_unknown_state(self):
        mgr = McpManager({})
        result = await mgr.complete_auth("unknown_state", "code123")
        assert result is False
