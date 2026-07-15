"""Source-aware local-only dependency regression tests."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import release_features
from app.auth.local import require_local_session
from app.auth.middleware import AuthMiddleware
from app.models.goal_run import GoalRun
from app.models.idempotency_record import IdempotencyRecord
from app.models.session import Session
from app.models.session_goal import SessionGoal


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


@pytest.mark.asyncio
async def test_real_goal_routes_reject_remote_bearer_without_db_side_effects(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Goal control stays local-only even when remote auth is future-enabled."""

    import app.auth.middleware as auth_middleware

    assert release_features.GOALS_RELEASED is True

    token_path = tmp_path / "remote_token.json"
    token_path.write_text(json.dumps({"token": REMOTE_TOKEN}), encoding="utf-8")
    settings = app_client.app.state.settings
    settings.remote_access_enabled = True
    settings.remote_token_path = str(token_path)
    monkeypatch.setattr(auth_middleware, "REMOTE_ACCESS_RELEASED", True)

    protected_session_id = "remote-guard-existing-session"
    attempted_session_id = "remote-guard-attempted-session"
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id=protected_session_id,
                    directory=".",
                    title="Protected Goal",
                    version="1.0.0",
                )
            )
            db.add(
                SessionGoal(
                    session_id=protected_session_id,
                    objective="This objective must not be exposed remotely",
                )
            )

    async def database_counts() -> tuple[int, int, int, int]:
        async with session_factory() as db:
            return (
                int((await db.execute(select(func.count(Session.id)))).scalar_one()),
                int(
                    (
                        await db.execute(select(func.count(SessionGoal.id)))
                    ).scalar_one()
                ),
                int((await db.execute(select(func.count(GoalRun.id)))).scalar_one()),
                int(
                    (
                        await db.execute(select(func.count(IdempotencyRecord.id)))
                    ).scalar_one()
                ),
            )

    counts_before = await database_counts()
    transport = ASGITransport(
        app=app_client.app,
        client=("198.51.100.23", 43123),
    )
    async with AsyncClient(
        transport=transport,
        base_url="http://remote.test",
        headers={"Authorization": f"Bearer {REMOTE_TOKEN}"},
    ) as remote_client:
        read_response = await remote_client.get(
            f"/api/sessions/{protected_session_id}/goal"
        )
        start_response = await remote_client.post(
            "/api/chat/goal",
            json={
                "client_request_id": "remote-goal-start-attempt",
                "session_id": attempted_session_id,
                "objective": "This Goal must never be admitted remotely",
            },
        )

    expected_detail = "This endpoint requires the local desktop session"
    assert read_response.status_code == 403
    assert read_response.json()["detail"] == expected_detail
    assert start_response.status_code == 403
    assert start_response.json()["detail"] == expected_detail
    assert await database_counts() == counts_before
    async with session_factory() as db:
        assert await db.get(Session, attempted_session_id) is None
        assert (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.request_key == "remote-goal-start-attempt"
                )
            )
        ).scalar_one_or_none() is None
