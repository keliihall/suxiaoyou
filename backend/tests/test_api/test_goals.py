from __future__ import annotations

import pytest
from sqlalchemy import select

from app import release_features
from app.agent.permission import (
    evaluate,
    parse_permission_snapshot,
    serialize_permission_snapshot,
)
from app.api import goals as goals_api
from app.models.goal_run import GoalRun
from app.models.idempotency_record import IdempotencyRecord
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.agent import PermissionRule, Ruleset

pytestmark = pytest.mark.asyncio


async def _seed_session(session_factory, session_id: str = "api-goal-session") -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id=session_id,
                    directory=".",
                    title="API Goal",
                    version="1.0.0",
                )
            )


def _mount_goal_router(app_client, monkeypatch, *, released: bool) -> None:
    monkeypatch.setattr(release_features, "GOALS_RELEASED", released)
    app_client.app.include_router(goals_api.router, prefix="/api")


async def test_goal_router_remains_closed_even_if_accidentally_mounted(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=False)
    response = await app_client.get("/api/sessions/anything/goal")
    assert response.status_code == 404


async def test_goal_api_lifecycle_revision_and_idempotency(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    await _seed_session(session_factory)
    path = "/api/sessions/api-goal-session/goal"
    create_body = {
        "client_request_id": "api-goal-create",
        "objective": "Deliver a stable Goal API",
        "definition_of_done": "Lifecycle tests pass",
    }
    created = await app_client.post(path, json=create_body)
    assert created.status_code == 201
    goal = created.json()
    assert goal["revision"] == 1
    assert goal["status"] == "active"
    assert goal["run_state"] == "idle"
    assert goal["token_budget"] is None
    assert "permission_snapshot" not in goal
    replay = await app_client.post(path, json=create_body)
    assert replay.status_code == 201
    assert replay.json()["id"] == goal["id"]
    conflict = await app_client.post(
        path,
        json={**create_body, "objective": "Reuse the key incorrectly"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    fetched = await app_client.get(path)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == goal["id"]
    usage = await app_client.get(f"{path}/usage")
    assert usage.status_code == 200
    assert usage.json() == {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "unattributed": 0,
        "total_tokens": 0,
        "source_count": 0,
    }
    updated = await app_client.patch(
        path,
        json={
            "client_request_id": "api-goal-update",
            "expected_revision": 1,
            "definition_of_done": "API and migration tests pass",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    stale = await app_client.patch(
        path,
        json={
            "client_request_id": "api-goal-stale-update",
            "expected_revision": 1,
            "objective": "Overwrite a newer revision",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "goal_revision_conflict",
        "message": "Goal revision changed (expected 1, current 2)",
        "expected_revision": 1,
        "current_revision": 2,
    }

    paused = await app_client.post(
        f"{path}/pause",
        json={
            "client_request_id": "api-goal-pause",
            "expected_revision": 2,
        },
    )
    assert (paused.json()["status"], paused.json()["revision"]) == ("paused", 3)
    cleared = await app_client.delete(
        path,
        params={
            "client_request_id": "api-goal-clear",
            "expected_revision": 3,
        },
    )
    assert cleared.status_code == 204
    duplicate_clear = await app_client.delete(
        path,
        params={
            "client_request_id": "api-goal-clear",
            "expected_revision": 3,
        },
    )
    assert duplicate_clear.status_code == 204
    assert (await app_client.get(path)).json() is None


async def test_goal_api_accepts_large_optional_token_budget_without_product_ceiling(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    await _seed_session(session_factory, "api-goal-budget-cap")
    path = "/api/sessions/api-goal-budget-cap/goal"
    created = await app_client.post(
        path,
        json={
            "client_request_id": "api-goal-cap-create",
            "objective": "Allow a long-running token budget",
        },
    )
    assert created.status_code == 201

    accepted = await app_client.patch(
        path,
        json={
            "client_request_id": "api-goal-large-budget",
            "expected_revision": 1,
            "token_budget": 2_000_000,
        },
    )
    assert accepted.status_code == 200
    assert (accepted.json()["revision"], accepted.json()["token_budget"]) == (
        2,
        2_000_000,
    )

    enlarged = await app_client.patch(
        path,
        json={
            "client_request_id": "api-goal-larger-budget",
            "expected_revision": 2,
            "token_budget": 20_000_000,
        },
    )
    assert enlarged.status_code == 200
    assert (enlarged.json()["revision"], enlarged.json()["token_budget"]) == (
        3,
        20_000_000,
    )

    unlimited = await app_client.patch(
        path,
        json={
            "client_request_id": "api-goal-unlimited-budget",
            "expected_revision": 3,
            "token_budget": None,
        },
    )
    assert unlimited.status_code == 200
    assert unlimited.json()["token_budget"] is None
    current = (await app_client.get(path)).json()
    assert (current["revision"], current["token_budget"]) == (4, None)


async def test_goal_api_combined_content_limit_and_missing_session(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    await _seed_session(session_factory)
    too_long = await app_client.post(
        "/api/sessions/api-goal-session/goal",
        json={
            "client_request_id": "api-too-long-goal",
            "objective": "x" * 3000,
            "definition_of_done": "y" * 1001,
        },
    )
    assert too_long.status_code == 422
    missing = await app_client.post(
        "/api/sessions/missing/goal",
        json={
            "client_request_id": "api-missing-goal",
            "objective": "No session",
        },
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "goal_not_found"


async def test_atomic_goal_start_is_idempotent_and_reserves_before_worker(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    monkeypatch.setattr(
        release_features, "AUTONOMOUS_GOALS_RELEASED", False
    )
    started: list[tuple[str, str, str, str, bool]] = []

    async def fake_runner(job, request, *, initial_run_id, **kwargs):
        started.append(
            (
                job.session_id,
                job.goal_id,
                initial_run_id,
                request.text,
                kwargs["initial_skip_user_message"],
            )
        )
        job.complete()

    monkeypatch.setattr(goals_api, "run_goal_generation", fake_runner)
    body = {
        "client_request_id": "atomic-goal-start",
        "session_id": "atomic-goal-session",
        "objective": "Ship an atomic autonomous Goal",
        "definition_of_done": "The GoalRun is reserved before execution",
        "model": "fake-model",
    }
    response = await app_client.post("/api/chat/goal", json=body)
    assert response.status_code == 201
    payload = response.json()
    assert payload["session_id"] == "atomic-goal-session"
    assert payload["goal"]["run_state"] == "reserved"
    assert payload["goal"]["revision"] == 2
    assert payload["run"]["status"] == "reserved"

    # Give the installed task one event-loop turn; durable reservation already
    # existed before this worker was scheduled.
    import asyncio

    await asyncio.sleep(0)
    assert started == [
        (
            "atomic-goal-session",
            payload["goal"]["id"],
            payload["run"]["id"],
            body["objective"],
            False,
        )
    ]

    replay = await app_client.post("/api/chat/goal", json=body)
    assert replay.status_code == 201
    assert replay.json() == payload
    conflict = await app_client.post(
        "/api/chat/goal",
        json={**body, "objective": "Reuse the key for a different Goal"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    async with session_factory() as db:
        goal = (
            await db.execute(
                select(SessionGoal).where(
                    SessionGoal.session_id == "atomic-goal-session"
                )
            )
        ).scalar_one()
        runs = list(
            (
                await db.execute(select(GoalRun).where(GoalRun.goal_id == goal.id))
            ).scalars()
        )
    assert len(runs) == 1


async def test_atomic_goal_validation_failure_leaves_no_orphan_session(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    response = await app_client.post(
        "/api/chat/goal",
        json={
            "client_request_id": "atomic-invalid-image",
            "session_id": "atomic-invalid-session",
            "objective": "This must not leave an orphan",
            "model": "not-a-vision-model",
            "attachments": [
                {
                    "name": "image.png",
                    "path": "/tmp/image.png",
                    "mime_type": "image/png",
                }
            ],
        },
    )
    assert response.status_code == 400
    async with session_factory() as db:
        assert await db.get(Session, "atomic-invalid-session") is None


async def test_autonomous_resume_returns_new_stream_and_is_idempotent(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    monkeypatch.setattr(
        release_features, "AUTONOMOUS_GOALS_RELEASED", False
    )
    await _seed_session(session_factory, "resume-goal-session")
    async with session_factory() as db:
        async with db.begin():
            session = await db.get(Session, "resume-goal-session")
            assert session is not None
            session.permission_snapshot = serialize_permission_snapshot(
                Ruleset(rules=[
                    PermissionRule(action="allow", permission="*"),
                    PermissionRule(action="allow", permission="web_search"),
                ])
            )
    created = await app_client.post(
        "/api/sessions/resume-goal-session/goal",
        json={
            "client_request_id": "resume-goal-create",
            "objective": "Resume into a durable run",
        },
    )
    assert created.status_code == 201
    async with session_factory() as db:
        async with db.begin():
            session = await db.get(Session, "resume-goal-session")
            assert session is not None
            session.permission_snapshot = serialize_permission_snapshot(
                Ruleset(rules=[
                    PermissionRule(action="allow", permission="*"),
                    PermissionRule(action="deny", permission="web_search"),
                ])
            )
    started: list[str] = []

    async def fake_runner(job, request, *, initial_run_id, **kwargs):
        del request, kwargs
        started.append(initial_run_id)
        job.complete()

    monkeypatch.setattr(goals_api, "run_goal_generation", fake_runner)
    body = {
        "client_request_id": "resume-goal-run-request",
        "expected_revision": created.json()["revision"],
    }
    response = await app_client.post(
        "/api/sessions/resume-goal-session/goal/resume",
        json=body,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stream_id"]
    assert payload["goal"]["run_state"] == "reserved"
    assert payload["run"]["trigger"] == "resume"

    import asyncio

    await asyncio.sleep(0)
    assert started == [payload["run"]["id"]]
    replay = await app_client.post(
        "/api/sessions/resume-goal-session/goal/resume",
        json=body,
    )
    assert replay.status_code == 200
    assert replay.json() == payload

    async with session_factory() as db:
        runs = list((await db.execute(select(GoalRun))).scalars())
        goal = (
            await db.execute(
                select(SessionGoal).where(
                    SessionGoal.session_id == "resume-goal-session"
                )
            )
        ).scalar_one()
    assert len(runs) == 1
    resumed_permissions = parse_permission_snapshot(goal.permission_snapshot)
    assert resumed_permissions is not None
    assert evaluate("web_search", "*", resumed_permissions) == "deny"


async def test_interrupted_goal_start_replay_requires_explicit_review(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    monkeypatch.setattr(
        release_features, "AUTONOMOUS_GOALS_RELEASED", False
    )

    async def fake_runner(job, request, **kwargs):
        del request, kwargs
        job.complete()

    monkeypatch.setattr(goals_api, "run_goal_generation", fake_runner)
    body = {
        "client_request_id": "interrupted-goal-start",
        "session_id": "interrupted-goal-session",
        "objective": "Do not replay uncertain autonomous work",
    }
    started = await app_client.post("/api/chat/goal", json=body)
    assert started.status_code == 201
    async with session_factory() as db:
        async with db.begin():
            record = (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope == "chat.goal",
                        IdempotencyRecord.request_key == body["client_request_id"],
                    )
                )
            ).scalar_one()
            record.status = "interrupted"

    replay = await app_client.post("/api/chat/goal", json=body)

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "idempotency_interrupted"
    async with session_factory() as db:
        assert len(list((await db.execute(select(GoalRun))).scalars())) == 1


async def test_failed_goal_resume_replay_requires_explicit_review(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mount_goal_router(app_client, monkeypatch, released=True)
    monkeypatch.setattr(
        release_features, "AUTONOMOUS_GOALS_RELEASED", False
    )
    await _seed_session(session_factory, "failed-resume-session")
    created = await app_client.post(
        "/api/sessions/failed-resume-session/goal",
        json={
            "client_request_id": "failed-resume-create",
            "objective": "Resume exactly once",
        },
    )
    assert created.status_code == 201

    async def fake_runner(job, request, **kwargs):
        del request, kwargs
        job.complete()

    monkeypatch.setattr(goals_api, "run_goal_generation", fake_runner)
    body = {
        "client_request_id": "failed-resume-request",
        "expected_revision": created.json()["revision"],
    }
    resumed = await app_client.post(
        "/api/sessions/failed-resume-session/goal/resume",
        json=body,
    )
    assert resumed.status_code == 200
    scope = "goal.resume.run:failed-resume-session"
    async with session_factory() as db:
        async with db.begin():
            record = (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope == scope,
                        IdempotencyRecord.request_key == body["client_request_id"],
                    )
                )
            ).scalar_one()
            record.status = "failed"

    replay = await app_client.post(
        "/api/sessions/failed-resume-session/goal/resume",
        json=body,
    )

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "idempotency_interrupted"
    async with session_factory() as db:
        assert len(list((await db.execute(select(GoalRun))).scalars())) == 1
