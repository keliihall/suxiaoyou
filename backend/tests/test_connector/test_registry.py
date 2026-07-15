"""Tests for app.connector.registry — MCP connector deduplication."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.api import google_auth
from app.connector.registry import ConnectorPersistenceError, ConnectorRegistry
from app.mcp.local_approval import LocalMcpApprovalResult
from app.security.control import (
    SecurityControl,
    get_security_control,
    set_security_control,
)


class TestNormalizeUrl:
    def test_strips_trailing_slash(self):
        assert ConnectorRegistry._normalize_url("https://api.com/") == "https://api.com"

    def test_lowercases_host(self):
        assert ConnectorRegistry._normalize_url("https://API.COM/path") == "https://api.com/path"

    def test_preserves_path(self):
        assert ConnectorRegistry._normalize_url("https://api.com/v1/sse") == "https://api.com/v1/sse"

    def test_handles_no_path(self):
        assert ConnectorRegistry._normalize_url("https://api.com") == "https://api.com"


class TestRegisterFromPlugin:
    def _make_registry(self, tmp_path: Path) -> ConnectorRegistry:
        # Patch catalog loading to avoid missing data file
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            return ConnectorRegistry(project_dir=str(tmp_path))

    def test_creates_connector(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        ids = reg.register_from_plugin("myplugin", {
            "slack": {"url": "https://slack.mcp.io/sse", "type": "remote"},
        })
        assert "slack" in ids
        c = reg.get("slack")
        assert c is not None
        assert c.url == "https://slack.mcp.io/sse"
        assert c.source == "custom"

    def test_dedup_by_url(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        reg.register_from_plugin("plugin-a", {
            "slack": {"url": "https://slack.mcp.io/sse", "type": "remote"},
        })
        reg.register_from_plugin("plugin-b", {
            "slack": {"url": "https://slack.mcp.io/sse", "type": "remote"},
        })
        connectors = reg.list_connectors()
        slack_connectors = [c for c in connectors if c.id == "slack"]
        assert len(slack_connectors) == 1
        assert "plugin-a" in slack_connectors[0].referenced_by
        assert "plugin-b" in slack_connectors[0].referenced_by

    def test_strips_namespace(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        ids = reg.register_from_plugin("eng", {
            "engineering:slack": {"url": "https://slack.mcp.io/sse", "type": "remote"},
        })
        assert "slack" in ids

    def test_skips_remote_without_url(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        ids = reg.register_from_plugin("p", {
            "nourl": {"type": "remote"},
        })
        assert ids == []


class TestRegisterCustom:
    def _make_registry(self, tmp_path: Path) -> ConnectorRegistry:
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            return ConnectorRegistry(project_dir=str(tmp_path))

    def test_creates_custom_connector(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        c = reg.register_custom("my-tool", "My Tool", "https://my.tool/sse")
        assert c.id == "my-tool"
        assert c.source == "custom"

    def test_duplicate_id_raises(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        reg.register_custom("my-tool", "My Tool", "https://my.tool/sse")
        with pytest.raises(ValueError):
            reg.register_custom("my-tool", "My Tool 2", "https://my.tool2/sse")

    def test_persistence_failure_does_not_register_connector(
        self, tmp_path: Path
    ) -> None:
        reg = self._make_registry(tmp_path)
        with patch(
            "app.connector.registry.atomic_write_text",
            side_effect=OSError("disk is full"),
        ):
            with pytest.raises(ConnectorPersistenceError, match="could not be saved"):
                reg.register_custom("my-tool", "My Tool", "https://my.tool/sse")

        assert reg.get("my-tool") is None
        assert reg._persisted_state == {"enabled": [], "custom": []}


class TestRemoveCustom:
    def _make_registry(self, tmp_path: Path) -> ConnectorRegistry:
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            return ConnectorRegistry(project_dir=str(tmp_path))

    def test_removes_custom(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        reg.register_custom("my-tool", "My Tool", "https://my.tool/sse")
        assert reg.remove_custom("my-tool") is True
        assert reg.get("my-tool") is None

    def test_returns_false_for_builtin(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        reg.register_from_plugin(
            "p",
            {"slack": {"url": "https://s.io", "type": "remote"}},
            source="builtin",
        )
        assert reg.remove_custom("slack") is False

    def test_returns_false_for_nonexistent(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        assert reg.remove_custom("nope") is False

    def test_persistence_failure_does_not_remove_connector(
        self, tmp_path: Path
    ) -> None:
        reg = self._make_registry(tmp_path)
        reg.register_custom("my-tool", "My Tool", "https://my.tool/sse")
        before = reg._persisted_state.copy()

        with patch(
            "app.connector.registry.atomic_write_text",
            side_effect=OSError("read-only filesystem"),
        ):
            with pytest.raises(ConnectorPersistenceError, match="could not be saved"):
                reg.remove_custom("my-tool")

        assert reg.get("my-tool") is not None
        assert reg._persisted_state == before


class TestConnectorStatePersistence:
    def _make_registry(self, tmp_path: Path) -> ConnectorRegistry:
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            registry = ConnectorRegistry(project_dir=str(tmp_path))
        registry.register_from_plugin(
            "plugin",
            {"remote": {"url": "https://example.com/mcp", "type": "remote"}},
        )
        return registry

    @pytest.mark.asyncio
    async def test_enable_failure_keeps_runtime_disabled(self, tmp_path: Path) -> None:
        registry = self._make_registry(tmp_path)
        connector = registry.get("remote")
        assert connector is not None
        manager = MagicMock()
        manager._config = {"remote": {"enabled": False}}
        manager.reconnect = AsyncMock(return_value=True)
        registry._mcp_manager = manager

        with patch(
            "app.connector.registry.atomic_write_text",
            side_effect=OSError("disk is full"),
        ):
            with pytest.raises(ConnectorPersistenceError, match="could not be saved"):
                await registry.enable("remote")

        assert connector.enabled is False
        assert "remote" not in registry._persisted_state["enabled"]
        assert manager._config["remote"]["enabled"] is False
        manager.reconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disable_failure_keeps_runtime_enabled(self, tmp_path: Path) -> None:
        registry = self._make_registry(tmp_path)
        connector = registry.get("remote")
        assert connector is not None
        connector.enabled = True
        registry._persisted_state["enabled"] = ["remote"]
        manager = MagicMock()
        manager._config = {"remote": {"enabled": True}}
        manager.disable = AsyncMock(return_value=True)
        registry._mcp_manager = manager

        with patch(
            "app.connector.registry.atomic_write_text",
            side_effect=OSError("read-only filesystem"),
        ):
            with pytest.raises(ConnectorPersistenceError, match="could not be saved"):
                await registry.disable("remote")

        assert connector.enabled is True
        assert registry._persisted_state["enabled"] == ["remote"]
        assert manager._config["remote"]["enabled"] is True
        manager.disable.assert_not_awaited()


class TestListAndGet:
    def _make_registry(self, tmp_path: Path) -> ConnectorRegistry:
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            return ConnectorRegistry(project_dir=str(tmp_path))

    def test_list_sorted_by_name(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        reg.register_custom("zoom", "Zoom", "https://z.io")
        reg.register_custom("asana", "Asana", "https://a.io")
        reg.register_custom("slack", "Slack", "https://s.io")
        names = [c.name for c in reg.list_connectors()]
        assert names == ["Asana", "Slack", "Zoom"]

    def test_get_existing(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        reg.register_custom("my-tool", "My Tool", "https://my.tool/sse")
        assert reg.get("my-tool") is not None

    def test_get_nonexistent(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        assert reg.get("nope") is None


class TestLifecycle:
    def _make_registry(self, tmp_path: Path) -> ConnectorRegistry:
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            return ConnectorRegistry(project_dir=str(tmp_path))

    def test_prepare_builds_manager_without_connecting(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        with patch("app.connector.registry.McpManager") as manager_type:
            manager = MagicMock()
            manager.startup = AsyncMock()
            manager_type.return_value = manager

            reg.prepare()

        assert reg.mcp_manager is manager
        manager.startup.assert_not_awaited()

    def test_prepare_propagates_server_owned_connector_provenance(
        self,
        tmp_path: Path,
    ) -> None:
        reg = self._make_registry(tmp_path)
        reg.register_from_plugin(
            "bundled",
            {"trusted": {"url": "https://trusted.example/mcp", "type": "remote"}},
            source="builtin",
        )
        reg.register_from_plugin(
            "project",
            {"user-remote": {"url": "https://user.example/mcp", "type": "remote"}},
            source="project",
        )
        reg.register_from_plugin(
            "project-local",
            {
                "user-local": {
                    "type": "local",
                    "command": ["user-server"],
                    "connector_provenance": "builtin",
                }
            },
            source="custom",
        )

        reg.prepare()
        manager = reg.mcp_manager
        assert manager is not None
        assert manager._config["trusted"]["connector_provenance"] == "builtin"
        assert manager._config["user-remote"]["connector_provenance"] == "custom"
        assert manager._config["user-local"]["connector_provenance"] == "custom"

    @pytest.mark.asyncio
    async def test_connect_enabled_syncs_tools_after_connection(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        reg.prepare()
        manager = reg.mcp_manager
        assert manager is not None
        manager.startup = AsyncMock()  # type: ignore[method-assign]
        reg.sync_tools = MagicMock()  # type: ignore[method-assign]

        await reg.connect_enabled()

        manager.startup.assert_awaited_once()
        reg.sync_tools.assert_called_once()


class TestLocalStartupApproval:
    def _make_registry(self, tmp_path: Path) -> ConnectorRegistry:
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            registry = ConnectorRegistry(project_dir=str(tmp_path))
        registry.register_from_plugin(
            "test",
            {
                "local": {
                    "type": "local",
                    "command": ["local-server", "--reviewed"],
                }
            },
        )
        connector = registry.get("local")
        assert connector is not None
        connector.enabled = True
        return registry

    def test_status_surfaces_pending_connection_approval(self, tmp_path: Path) -> None:
        registry = self._make_registry(tmp_path)
        manager = MagicMock()
        manager.status.return_value = {}
        manager.local_startup_approval.return_value = {
            "required": True,
            "approved": False,
            "fingerprint": "sha256:" + "a" * 64,
            "command": ["local-server", "--reviewed"],
            "cwd": "/private/app-data",
            "environment_keys": ["PATH"],
            "error": None,
        }
        registry._mcp_manager = manager

        status = registry.status()["local"]

        assert status["status"] == "needs_approval"
        assert status["connected"] is False
        assert status["local_approval"]["required"] is True

    @pytest.mark.asyncio
    async def test_explicit_approval_delegates_and_syncs_tools(
        self,
        tmp_path: Path,
    ) -> None:
        registry = self._make_registry(tmp_path)
        manager = MagicMock()
        expected = LocalMcpApprovalResult(True, True, "connected")
        manager.approve_local_startup = AsyncMock(return_value=expected)
        registry._mcp_manager = manager
        registry.sync_tools = MagicMock()  # type: ignore[method-assign]
        fingerprint = "sha256:" + "a" * 64

        with patch("app.connector.registry._external_runtime_stopped", return_value=False):
            approved = await registry.approve_local_startup("local", fingerprint)

        assert approved == expected
        manager.approve_local_startup.assert_awaited_once_with(
            "local",
            fingerprint,
        )
        registry.sync_tools.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_disabled_or_remote_connector_cannot_use_local_approval(
        self,
        tmp_path: Path,
    ) -> None:
        registry = self._make_registry(tmp_path)
        connector = registry.get("local")
        assert connector is not None
        connector.enabled = False
        manager = MagicMock()
        manager.approve_local_startup = AsyncMock(return_value=True)
        registry._mcp_manager = manager

        result = await registry.approve_local_startup(
            "local",
            "sha256:" + "a" * 64,
        )
        assert result.approval_persisted is False
        assert result.connected is False
        assert result.status == "blocked"
        manager.approve_local_startup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_emergency_transition_blocks_approval_before_manager_start(
        self,
        tmp_path: Path,
    ) -> None:
        registry = self._make_registry(tmp_path)
        manager = MagicMock()
        manager.approve_local_startup = AsyncMock(
            return_value=LocalMcpApprovalResult(True, True, "connected")
        )
        registry._mcp_manager = manager
        control = SecurityControl(tmp_path / "security-state.json")
        try:
            previous = get_security_control()
        except RuntimeError:
            previous = None
        set_security_control(control)

        await control.transition_lock.acquire()
        try:
            task = asyncio.create_task(
                registry.approve_local_startup(
                    "local",
                    "sha256:" + "a" * 64,
                )
            )
            await asyncio.sleep(0)
            manager.approve_local_startup.assert_not_awaited()
            await control.set_emergency_stop(True)
        finally:
            control.transition_lock.release()

        try:
            result = await asyncio.wait_for(task, timeout=1)
            assert result.connected is False
            assert result.approval_persisted is False
            manager.approve_local_startup.assert_not_awaited()
        finally:
            if previous is not None:
                set_security_control(previous)
            else:
                set_security_control(SecurityControl(tmp_path / "restored-state.json"))


class TestGoogleRuntimeStateMachine:
    def _make_registry(
        self,
        tmp_path: Path,
        *,
        enabled: bool = True,
    ) -> tuple[ConnectorRegistry, MagicMock, MagicMock]:
        with patch.object(ConnectorRegistry, "_load_catalog", return_value={}):
            registry = ConnectorRegistry(project_dir=str(tmp_path))
        registry.register_from_plugin(
            "test",
            {
                "google-workspace": {
                    "type": "local",
                    "command": ["google-workspace-worker"],
                }
            },
        )
        connector = registry.get("google-workspace")
        assert connector is not None
        connector.enabled = enabled

        manager = MagicMock()
        manager.reconnect = AsyncMock(return_value=True)
        manager.disconnect_auth = AsyncMock(return_value=True)
        manager.disable = AsyncMock(return_value=True)
        client = MagicMock()
        client.close = AsyncMock()
        manager._clients = {"google-workspace": client}
        registry._mcp_manager = manager
        registry.sync_tools = MagicMock()  # type: ignore[method-assign]
        registry._inject_local_credentials = MagicMock()  # type: ignore[method-assign]
        return registry, manager, client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["enable", "disable", "reconnect"])
    async def test_all_public_google_runtime_mutations_share_one_lock(
        self,
        tmp_path: Path,
        operation: str,
    ) -> None:
        registry, manager, client = self._make_registry(
            tmp_path,
            enabled=operation != "enable",
        )
        await registry.google_auth_operation_lock.acquire()
        task = asyncio.create_task(
            getattr(registry, operation)("google-workspace")
        )
        await asyncio.sleep(0)

        assert not task.done()
        manager.reconnect.assert_not_awaited()
        manager.disable.assert_not_awaited()

        registry.google_auth_operation_lock.release()
        assert await asyncio.wait_for(task, timeout=1) is True

    @pytest.mark.asyncio
    async def test_google_disconnect_fences_then_deletes_inside_runtime_lock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        registry, manager, _client = self._make_registry(tmp_path)
        order: list[str] = []
        monkeypatch.setattr(
            google_auth,
            "fence_google_auth_disconnect",
            lambda project: order.append(f"fence:{project}"),
        )
        monkeypatch.setattr(
            google_auth,
            "delete_google_tokens",
            lambda project: order.append(f"delete:{project}"),
        )

        async def disconnect_runtime(name: str) -> bool:
            assert registry.google_auth_operation_lock.locked()
            order.append(f"runtime:{name}")
            return True

        manager.disconnect_auth.side_effect = disconnect_runtime

        assert await registry.disconnect("google-workspace") is True
        assert order == [
            f"fence:{tmp_path}",
            "runtime:google-workspace",
            f"delete:{tmp_path}",
        ]
        registry._inject_local_credentials.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_google_disable_fences_pending_callback_before_waiting(
        self,
        tmp_path: Path,
    ) -> None:
        registry, _manager, _client = self._make_registry(tmp_path)
        project_dir = str(tmp_path)
        scope = google_auth._credential_namespace(project_dir)
        generation = google_auth._auth_generations.get(scope, 0)
        state = f"disable-pending-{tmp_path.name}"
        google_auth._pending_states[state] = {
            "scope": scope,
            "generation": generation,
            "project_dir": project_dir,
            "redirect_uri": "http://localhost/callback",
        }

        await registry.google_auth_operation_lock.acquire()
        task = asyncio.create_task(registry.disable("google-workspace"))
        for _ in range(10):
            if google_auth._auth_generations.get(scope, 0) != generation:
                break
            await asyncio.sleep(0)

        assert not task.done()
        assert state not in google_auth._pending_states
        assert google_auth._auth_generations[scope] == generation + 1

        registry.google_auth_operation_lock.release()
        assert await asyncio.wait_for(task, timeout=1) is True
