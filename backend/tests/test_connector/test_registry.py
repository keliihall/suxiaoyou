"""Tests for app.connector.registry — MCP connector deduplication."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.api import google_auth
from app.connector.registry import ConnectorRegistry


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
        reg.register_from_plugin("p", {"slack": {"url": "https://s.io", "type": "remote"}})
        assert reg.remove_custom("slack") is False

    def test_returns_false_for_nonexistent(self, tmp_path: Path):
        reg = self._make_registry(tmp_path)
        assert reg.remove_custom("nope") is False


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
        client.close.assert_not_awaited()

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
