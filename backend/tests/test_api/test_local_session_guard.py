"""Source-aware local-only dependency regression tests."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.local import require_local_session
from app.auth.middleware import AuthMiddleware


SESSION_TOKEN = "suxiaoyou_st_test_local_guard_abcdef0123456789"
REMOTE_TOKEN = "suxiaoyou_rt_test_local_guard_abcdef0123456789"


@pytest.mark.asyncio
async def test_loopback_remote_bearer_cannot_pass_local_session_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.auth.middleware as auth_middleware
    import app.config as config_module

    token_path = tmp_path / "remote_token.json"
    token_path.write_text(json.dumps({"token": REMOTE_TOKEN}), encoding="utf-8")
    settings = types.SimpleNamespace(
        remote_access_enabled=True,
        remote_token_path=str(token_path),
        rate_limit_max_requests=120,
        rate_limit_max_failed_auth=5,
    )
    # Exercise the future-enabled credential classification while keeping the
    # release default itself closed everywhere else.
    monkeypatch.setattr(auth_middleware, "REMOTE_ACCESS_RELEASED", True)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    app = FastAPI()

    @app.get("/api/local-only", dependencies=[Depends(require_local_session)])
    async def local_only():
        return {"ok": True}

    app.state.settings = settings
    app.state.session_token = SESSION_TOKEN
    app.add_middleware(AuthMiddleware)

    transport = ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        remote_response = await client.get(
            "/api/local-only",
            headers={"authorization": f"Bearer {REMOTE_TOKEN}"},
        )
        local_response = await client.get(
            "/api/local-only",
            headers={"authorization": f"Bearer {SESSION_TOKEN}"},
        )

    assert remote_response.status_code == 403
    assert local_response.status_code == 200
