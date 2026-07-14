"""Tests for remote access API endpoints."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import remote
from app.api.remote import get_or_create_tunnel_manager, router
from app.auth.tunnel import TunnelManager

pytestmark = pytest.mark.asyncio


class TestEnableRemote:
    async def test_recovers_when_bundled_cloudflared_is_invalid(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(remote, "REMOTE_ACCESS_RELEASED", True)
        bad_bin_dir = tmp_path / "data" / "bin"
        bad_bin_dir.mkdir(parents=True)

        bad_binary = bad_bin_dir / "cloudflared"
        bad_binary.write_text("not a real executable\n", encoding="utf-8")
        bad_binary.chmod(0o755)

        token_path = tmp_path / "data" / "remote_token.json"

        app = FastAPI()

        @app.middleware("http")
        async def mark_local_session(request, call_next):
            request.state.source = "local"
            return await call_next(request)

        app.include_router(router, prefix="/api")
        app.state.settings = SimpleNamespace(
            remote_token_path=str(token_path),
            remote_access_enabled=False,
            remote_tunnel_mode="cloudflare",
            remote_tunnel_url="",
            remote_permission_mode="auto",
            port=8000,
        )

        class TestTunnelManager(TunnelManager):
            def __init__(self, *args, bin_dir: Path | None = None, **kwargs):
                super().__init__(*args, bin_dir=bad_bin_dir, **kwargs)

            async def _download(self, target: Path) -> Path:
                target.write_text(
                    "#!/bin/sh\n"
                    "printf 'https://recovered.trycloudflare.com\\n'\n"
                    "sleep 1\n",
                    encoding="utf-8",
                )
                target.chmod(0o755)
                return target

        monkeypatch.setattr("app.auth.tunnel.shutil.which", lambda _: None)
        monkeypatch.setattr("app.auth.tunnel.TunnelManager", TestTunnelManager)

        transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/remote/enable")

        assert response.status_code == 200
        data = response.json()
        assert data["token"]
        assert data["tunnel_url"] == "https://recovered.trycloudflare.com"
        assert bad_binary.read_text(encoding="utf-8").startswith("#!/bin/sh")

        tunnel_mgr = app.state.tunnel_manager
        await tunnel_mgr.stop()
        assert tunnel_mgr._monitor_task is None


async def test_remote_management_api_is_hard_disabled_in_release(tmp_path: Path):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.settings = SimpleNamespace(
        remote_token_path=str(tmp_path / "remote_token.json"),
        remote_access_enabled=False,
        remote_tunnel_mode="cloudflare",
        remote_tunnel_url="",
        remote_permission_mode="deny",
        port=8000,
    )

    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/remote/enable")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Remote access is not available in this release"
    }


async def test_tunnel_manager_uses_runtime_port_and_syncs_allowlist(monkeypatch):
    app = FastAPI()
    app.state.settings = SimpleNamespace(port=17321)
    app.state.runtime_allowed_origins = set()

    created = {}

    class FakeTunnelManager:
        def __init__(self, *, backend_port, on_url_change):
            created["port"] = backend_port
            created["callback"] = on_url_change

    monkeypatch.setattr("app.auth.tunnel.TunnelManager", FakeTunnelManager)

    manager = get_or_create_tunnel_manager(app)
    assert manager is app.state.tunnel_manager
    assert created["port"] == 17321

    callback = created["callback"]
    callback(None, "https://first.trycloudflare.com")
    assert app.state.runtime_allowed_origins == {
        "https://first.trycloudflare.com"
    }
    callback(
        "https://first.trycloudflare.com",
        "https://second.trycloudflare.com",
    )
    assert app.state.runtime_allowed_origins == {
        "https://second.trycloudflare.com"
    }
