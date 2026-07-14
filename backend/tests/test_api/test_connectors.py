"""Tests for connector management API endpoints."""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.api import google_auth
from app.auth import credential_store
from app.auth.credential_store import CredentialStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _mock_cr(app_client):
    """Inject a richer mock ConnectorRegistry."""
    cr = MagicMock()
    cr.status.return_value = {
        "github": {"status": "connected", "error": None, "type": "remote", "tools": 3},
        "slack": {"status": "needs_auth", "error": None, "type": "remote", "tools": 0},
    }
    cr.enable = AsyncMock(return_value=True)
    cr.disable = AsyncMock(return_value=True)
    cr.reconnect = AsyncMock(return_value=True)
    cr.connect = AsyncMock(return_value={"auth_url": "https://ex.com/auth", "state": "abc"})
    cr.complete_auth = AsyncMock(return_value=True)
    cr.disconnect = AsyncMock(return_value=True)
    cr.google_auth_operation_lock = asyncio.Lock()
    cr.get.return_value = MagicMock(enabled=True)
    cr.mcp_manager = MagicMock(_clients={}, _token_store=MagicMock())

    conn = MagicMock()
    conn.to_dict.return_value = {"id": "c1", "name": "Custom"}
    cr.register_custom.return_value = conn
    cr.remove_custom.return_value = True

    app_client.app.state.connector_registry = cr
    return cr


class TestListConnectors:
    async def test_with_registry(self, app_client, _mock_cr):
        resp = await app_client.get("/api/connectors")
        assert resp.status_code == 200
        assert "github" in resp.json()["connectors"]

    async def test_no_registry(self, app_client):
        app_client.app.state.connector_registry = None
        resp = await app_client.get("/api/connectors")
        assert resp.status_code == 200
        assert resp.json() == {"connectors": {}}


class TestConnectorDetail:
    async def test_existing(self, app_client, _mock_cr):
        resp = await app_client.get("/api/connectors/github")
        assert resp.status_code == 200
        assert resp.json()["status"] == "connected"

    async def test_not_found(self, app_client, _mock_cr):
        resp = await app_client.get("/api/connectors/nonexistent")
        assert resp.status_code == 404


class TestAddCustom:
    async def test_success(self, app_client, _mock_cr):
        resp = await app_client.post("/api/connectors", json={
            "id": "c1", "name": "Custom", "url": "https://ex.com",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_duplicate(self, app_client, _mock_cr):
        _mock_cr.register_custom.side_effect = ValueError("Dup")
        resp = await app_client.post("/api/connectors", json={
            "id": "dup", "name": "D", "url": "https://ex.com",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is False


class TestRemoveCustom:
    async def test_success(self, app_client, _mock_cr):
        resp = await app_client.delete("/api/connectors/c1")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_not_custom(self, app_client, _mock_cr):
        _mock_cr.remove_custom.return_value = False
        resp = await app_client.delete("/api/connectors/builtin")
        assert resp.status_code == 200
        assert resp.json()["success"] is False


class TestEnableDisable:
    async def test_enable(self, app_client, _mock_cr):
        resp = await app_client.post("/api/connectors/github/enable")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_disable(self, app_client, _mock_cr):
        resp = await app_client.post("/api/connectors/slack/disable")
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestOAuthCallback:
    async def test_callback(self, app_client, _mock_cr):
        resp = await app_client.get("/api/connectors/oauth/callback", params={"code": "c", "state": "s"})
        assert resp.status_code == 200
        _mock_cr.complete_auth.assert_awaited_once_with("s", "c")


class TestOAuthStart:
    async def test_uses_actual_runtime_port(self, app_client, _mock_cr):
        app_client.app.state.settings.port = 17321

        resp = await app_client.post("/api/connectors/slack/connect")

        assert resp.status_code == 200
        _mock_cr.connect.assert_awaited_once_with(
            "slack",
            "http://localhost:17321/api/connectors/oauth/callback",
        )


class TestGoogleGenericEntrypoints:
    async def test_generic_token_and_oauth_paths_are_fail_closed(
        self,
        app_client,
        _mock_cr,
    ) -> None:
        token = await app_client.post(
            "/api/connectors/google-workspace/token",
            json={"token": "must-not-be-stored"},
        )
        connect = await app_client.post(
            "/api/connectors/google-workspace/connect"
        )
        callback = await app_client.post(
            "/api/connectors/google-workspace/auth-callback",
            json={"code": "code", "state": "state"},
        )

        assert token.json()["success"] is False
        assert connect.json()["success"] is False
        assert callback.json()["success"] is False
        _mock_cr.mcp_manager._token_store.save.assert_not_called()
        _mock_cr.connect.assert_not_awaited()
        _mock_cr.complete_auth.assert_not_awaited()
        _mock_cr.enable.assert_not_awaited()
        _mock_cr.reconnect.assert_not_awaited()

    async def test_legacy_mcp_fallback_cannot_mutate_google_runtime(
        self,
        app_client,
    ) -> None:
        manager = MagicMock()
        manager.reconnect = AsyncMock(return_value=True)
        manager.disconnect_auth = AsyncMock(return_value=True)
        app_client.app.state.connector_registry = None
        app_client.app.state.mcp_manager = manager

        reconnect = await app_client.post(
            "/api/mcp/google-workspace/reconnect"
        )
        disconnect = await app_client.post(
            "/api/mcp/google-workspace/disconnect"
        )

        assert reconnect.json()["success"] is False
        assert disconnect.json()["success"] is False
        manager.reconnect.assert_not_awaited()
        manager.disconnect_auth.assert_not_awaited()


class TestDisconnect:
    async def test_google_disconnect_delegates_to_registry_state_machine(
        self,
        app_client,
        _mock_cr,
    ) -> None:
        resp = await app_client.post("/api/connectors/google-workspace/disconnect")

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        _mock_cr.disconnect.assert_awaited_once_with("google-workspace")

    async def test_google_auth_runtime_transitions_are_generation_serialized(
        self,
        app_client,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        project_dir = str(tmp_path)
        settings = app_client.app.state.settings
        settings.project_dir = project_dir
        settings.google_client_id = "test-google-client"
        settings.google_client_secret = "test-google-secret"

        store = CredentialStore(
            fallback_path=tmp_path / "credential-fallback.json",
            native_backend=None,
        )
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)

        old_reconnect_started = asyncio.Event()
        release_old_reconnect = asyncio.Event()
        overlap_exchange_started = asyncio.Event()

        class TokenResponse:
            status_code = 200
            text = ""

            def __init__(self, code: str) -> None:
                self.code = code

            def json(self) -> dict[str, object]:
                return {
                    "access_token": f"access-{self.code}",
                    "refresh_token": f"refresh-{self.code}",
                    "expires_in": 3600,
                    "scope": "test",
                }

        class TokenClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            async def post(self, _url: str, *, data: dict[str, str]):
                if data["code"] == "overlap":
                    overlap_exchange_started.set()
                return TokenResponse(data["code"])

        monkeypatch.setattr(google_auth.httpx, "AsyncClient", TokenClient)

        class CoordinatedRegistry:
            def __init__(self) -> None:
                self.google_auth_operation_lock = asyncio.Lock()
                self.reconnect_count = 0
                self.disconnect_count = 0
                self.runtime_generation: int | None = None

            def _inject_local_credentials(self) -> None:
                pass

            async def reconnect_google_runtime_locked(self) -> bool:
                self.reconnect_count += 1
                if self.reconnect_count == 1:
                    old_reconnect_started.set()
                    await release_old_reconnect.wait()
                tokens = google_auth.load_google_tokens(project_dir)
                self.runtime_generation = (
                    tokens.get(google_auth._AUTH_GENERATION_FIELD)
                    if tokens
                    else None
                )
                return tokens is not None

            async def disconnect_google_runtime_locked(self) -> bool:
                self.disconnect_count += 1
                self.runtime_generation = None
                return True

            async def disconnect(self, connector_id: str) -> bool:
                assert connector_id == "google-workspace"
                google_auth.fence_google_auth_disconnect(project_dir)
                async with self.google_auth_operation_lock:
                    success = await self.disconnect_google_runtime_locked()
                    if success:
                        google_auth.delete_google_tokens(project_dir)
                        self._inject_local_credentials()
                    return success

            def status(self) -> dict[str, object]:
                return {}

        registry = CoordinatedRegistry()
        app_client.app.state.connector_registry = registry

        real_fence = google_auth.fence_google_auth_disconnect
        disconnect_fenced = asyncio.Event()

        def observed_fence(project: str | None) -> None:
            real_fence(project)
            disconnect_fenced.set()

        monkeypatch.setattr(google_auth, "fence_google_auth_disconnect", observed_fence)

        async def start_auth() -> str:
            response = await app_client.post("/api/google/auth-start")
            assert response.status_code == 200
            assert response.json()["success"] is True
            return response.json()["state"]

        old_state = await start_auth()
        old_callback = asyncio.create_task(
            app_client.get(
                "/api/google/callback",
                params={"code": "old", "state": old_state},
            )
        )
        await asyncio.wait_for(old_reconnect_started.wait(), timeout=1)

        disconnect = asyncio.create_task(
            app_client.post("/api/connectors/google-workspace/disconnect")
        )
        await asyncio.wait_for(disconnect_fenced.wait(), timeout=1)

        # This authorization begins after the fence but before disconnect has
        # committed. It must be ordered behind, and cancelled by, disconnect.
        overlap_state = await start_auth()
        overlap_callback = asyncio.create_task(
            app_client.get(
                "/api/google/callback",
                params={"code": "overlap", "state": overlap_state},
            )
        )
        await asyncio.wait_for(overlap_exchange_started.wait(), timeout=1)
        await asyncio.sleep(0)

        # Runtime disconnect cannot pass a callback transition already in the
        # critical section. This removes the late stale-cleanup window.
        assert not disconnect.done()
        release_old_reconnect.set()

        disconnect_response = await asyncio.wait_for(disconnect, timeout=1)
        assert disconnect_response.status_code == 200
        assert disconnect_response.json()["success"] is True

        overlap_response = await asyncio.wait_for(overlap_callback, timeout=1)
        assert "cancelled" in overlap_response.text
        assert not google_auth._get_token_path(project_dir).exists()

        # A generation started after disconnect commits is newer and must stay
        # connected even when the old callback task is awaited afterward.
        newest_state = await start_auth()
        newest_response = await app_client.get(
            "/api/google/callback",
            params={"code": "newest", "state": newest_state},
        )
        assert newest_response.status_code == 200
        assert "cancelled" not in newest_response.text

        old_response = await asyncio.wait_for(old_callback, timeout=1)
        assert "cancelled" in old_response.text
        scope = google_auth._credential_namespace(project_dir)
        assert registry.runtime_generation == google_auth._auth_generations[scope]
        assert registry.reconnect_count == 2
        assert registry.disconnect_count == 2
