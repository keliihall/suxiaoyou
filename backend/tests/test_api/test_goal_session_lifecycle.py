from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import release_features
from app.api import chat as chat_api
from app.api import goals as goals_api
from app.dependencies import get_stream_manager, set_stream_manager
from app.models.idempotency_record import IdempotencyRecord
from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.goal import GoalCreateRequest
from app.session.goal_manager import create_session_goal, get_session_goal
from app.streaming.manager import StreamManager

pytestmark = pytest.mark.asyncio


async def _create_api_session(app_client, title: str = "Goal lifecycle") -> str:
    response = await app_client.post("/api/sessions", json={"title": title})
    assert response.status_code == 201
    return response.json()["id"]


async def _seed_goal(session_factory, session_id: str) -> SessionGoal:
    async with session_factory() as db:
        async with db.begin():
            return await create_session_goal(
                db,
                session_id,
                GoalCreateRequest(
                    client_request_id=f"create-{session_id}",
                    objective="Protect the Goal lifecycle",
                ),
            )


async def test_archiving_session_pauses_active_goal_without_auto_resume(
    app_client,
    session_factory,
) -> None:
    session_id = await _create_api_session(app_client)
    seeded = await _seed_goal(session_factory, session_id)
    stream_manager = get_stream_manager()
    job = stream_manager.create_job(
        "archive-lifecycle-stream",
        session_id,
        invocation_source="goal",
        goal_id=seeded.id,
    )
    async with session_factory() as db:
        async with db.begin():
            goal = await db.get(SessionGoal, seeded.id)
            assert goal is not None
            goal.run_state = "running"

    try:
        archived_at = datetime.now(timezone.utc).isoformat()
        response = await app_client.patch(
            f"/api/sessions/{session_id}",
            json={"time_archived": archived_at},
        )
        assert response.status_code == 200
        assert response.json()["goal_status"] == "paused"
        assert response.json()["goal_run_state"] == "interrupted"
        assert response.json()["goal_needs_input"] is True
        assert job.execution_admission_open is False
        async with session_factory() as db:
            goal = await get_session_goal(db, session_id)
            assert goal is not None
            assert (goal.status, goal.run_state) == ("paused", "interrupted")
            assert goal.needs_review is True
            assert goal.revision == 2

        restored = await app_client.patch(
            f"/api/sessions/{session_id}",
            json={"time_archived": None},
        )
        assert restored.status_code == 200
        async with session_factory() as db:
            goal = await get_session_goal(db, session_id)
            assert goal is not None
            assert (goal.status, goal.revision) == ("paused", 2)
    finally:
        job.complete()
        stream_manager.remove_job(job.stream_id)


async def test_archived_session_explicitly_rejects_goal_resume(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "GOALS_RELEASED", True)
    app_client.app.include_router(goals_api.router, prefix="/api")
    session_id = await _create_api_session(app_client, "Archived Goal")
    await _seed_goal(session_factory, session_id)
    archived_at = datetime.now(timezone.utc).isoformat()
    archived = await app_client.patch(
        f"/api/sessions/{session_id}",
        json={"time_archived": archived_at},
    )
    assert archived.status_code == 200

    response = await app_client.post(
        f"/api/sessions/{session_id}/goal/resume",
        json={
            "client_request_id": "resume-archived-goal",
            "expected_revision": 2,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "session_archived",
        "message": "Unarchive this conversation before resuming its Goal.",
    }

    async with session_factory() as db:
        goal = await get_session_goal(db, session_id)
        session = await db.get(Session, session_id)
        records = list(
            (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope.in_(
                            (
                                f"goal.resume:{session_id}",
                                f"goal.resume.run:{session_id}",
                            )
                        )
                    )
                )
            ).scalars()
        )
    assert session is not None and session.time_archived is not None
    assert goal is not None
    assert (goal.status, goal.run_state, goal.revision) == (
        "paused",
        "idle",
        2,
    )
    assert records == []
    assert get_stream_manager().active_job_for_session(session_id) is None


async def test_deleting_session_cleans_all_goal_idempotency_scopes(
    app_client,
    session_factory,
) -> None:
    session_id = await _create_api_session(app_client)
    await _seed_goal(session_factory, session_id)
    goal_scopes = {
        f"goal.{operation}:{session_id}"
        for operation in ("create", "update", "pause", "resume", "clear")
    }
    async with session_factory() as db:
        async with db.begin():
            for index, scope in enumerate(sorted(goal_scopes - {f"goal.create:{session_id}"})):
                db.add(
                    IdempotencyRecord(
                        scope=scope,
                        request_key=f"lifecycle-request-{index}",
                        request_hash=f"hash-{index}",
                        status="completed",
                        response={"goal_id": "redacted"},
                    )
                )
            db.add(
                IdempotencyRecord(
                    scope="goal.create:another-session",
                    request_key="unrelated-request",
                    request_hash="unrelated-hash",
                    status="completed",
                    response={"goal_id": "unrelated"},
                )
            )

    response = await app_client.delete(f"/api/sessions/{session_id}")
    assert response.status_code == 200
    async with session_factory() as db:
        records = list((await db.execute(select(IdempotencyRecord))).scalars())
        assert all(record.scope not in goal_scopes for record in records)
        assert any(record.scope == "goal.create:another-session" for record in records)
        assert await get_session_goal(db, session_id) is None


async def test_deleting_session_waits_for_worker_to_quiesce_before_row_delete(
    app_client,
    session_factory,
) -> None:
    session_id = await _create_api_session(app_client, "Delete waits for Goal")
    stream_manager = get_stream_manager()
    job = stream_manager.create_job(
        "delete-lifecycle-stream",
        session_id,
        invocation_source="goal",
    )
    worker_observed_abort = asyncio.Event()
    allow_worker_exit = asyncio.Event()

    async def worker() -> None:
        await job.abort_event.wait()
        async with session_factory() as db:
            # The worker must be allowed to finish its final read/write phase
            # before the owning Session row is removed.
            assert await db.get(Session, session_id) is not None
        worker_observed_abort.set()
        await allow_worker_exit.wait()
        job.complete()

    job.task = asyncio.create_task(worker(), name="delete-lifecycle-worker")
    delete_task = asyncio.create_task(
        app_client.delete(f"/api/sessions/{session_id}"),
        name="delete-lifecycle-request",
    )
    try:
        await asyncio.wait_for(worker_observed_abort.wait(), timeout=1)
        assert delete_task.done() is False
        async with session_factory() as db:
            assert await db.get(Session, session_id) is not None

        allow_worker_exit.set()
        response = await asyncio.wait_for(delete_task, timeout=1)
        assert response.status_code == 200
        assert job.task.done()
        async with session_factory() as db:
            assert await db.get(Session, session_id) is None
    finally:
        allow_worker_exit.set()
        if not delete_task.done():
            delete_task.cancel()
        if not job.task.done():
            job.task.cancel()
        await asyncio.gather(delete_task, job.task, return_exceptions=True)
        stream_manager.remove_job(job.stream_id)


@pytest.mark.parametrize(
    "goal_status",
    ["active", "blocked", "usage_limited", "budget_limited"],
)
async def test_edit_and_resend_rejects_non_paused_goal_without_mutating_history(
    app_client,
    session_factory,
    goal_status: str,
) -> None:
    session_id = await _create_api_session(app_client)
    goal = await _seed_goal(session_factory, session_id)
    message_id = f"message-{goal_status}"
    part_id = f"part-{goal_status}"
    async with session_factory() as db:
        async with db.begin():
            stored_goal = await db.get(SessionGoal, goal.id)
            assert stored_goal is not None
            stored_goal.status = goal_status
            db.add(
                Message(
                    id=message_id,
                    session_id=session_id,
                    data={"role": "user"},
                )
            )
            db.add(
                Part(
                    id=part_id,
                    message_id=message_id,
                    session_id=session_id,
                    data={"type": "text", "text": "original"},
                )
            )

    response = await app_client.post(
        "/api/chat/edit",
        json={
            "session_id": session_id,
            "message_id": message_id,
            "text": "rewritten",
            "attachments": [],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "goal_history_edit_blocked"
    assert response.json()["detail"]["goal_status"] == goal_status
    async with session_factory() as db:
        part = await db.get(Part, part_id)
        assert part is not None
        assert part.data["text"] == "original"


async def test_edit_and_resend_holds_admission_lock_through_history_transaction(
    app_client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = StreamManager()
    set_stream_manager(manager)
    session_id = await _create_api_session(app_client, "Atomic edit admission")
    message_id = "atomic-edit-message"
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Message(
                    id=message_id,
                    session_id=session_id,
                    data={"role": "user"},
                )
            )
            db.add(
                Part(
                    id="atomic-edit-part",
                    message_id=message_id,
                    session_id=session_id,
                    data={"type": "text", "text": "original"},
                )
            )

    mutation_entered = asyncio.Event()
    allow_transaction_to_finish = asyncio.Event()
    contender_attempted = asyncio.Event()
    contender_entered = asyncio.Event()
    original_update = chat_api.update_message_text

    async def gated_update(db, requested_message_id: str, text: str) -> None:
        await original_update(db, requested_message_id, text)
        mutation_entered.set()
        await allow_transaction_to_finish.wait()

    async def fake_generation(job, _request, **_kwargs) -> None:
        job.complete()

    async def competing_admission() -> None:
        contender_attempted.set()
        async with manager.job_admission_lock:
            contender_entered.set()

    monkeypatch.setattr(chat_api, "update_message_text", gated_update)
    monkeypatch.setattr(chat_api, "run_generation", fake_generation)
    edit_task = asyncio.create_task(
        app_client.post(
            "/api/chat/edit",
            json={
                "session_id": session_id,
                "message_id": message_id,
                "text": "rewritten",
                "attachments": [],
            },
        ),
        name="atomic-history-edit",
    )
    contender_task: asyncio.Task[None] | None = None
    admitted_stream_id: str | None = None
    try:
        await asyncio.wait_for(mutation_entered.wait(), timeout=1)
        assert manager.job_admission_lock.locked()

        contender_task = asyncio.create_task(
            competing_admission(),
            name="competing-chat-admission",
        )
        await asyncio.wait_for(contender_attempted.wait(), timeout=1)
        await asyncio.sleep(0)
        assert contender_entered.is_set() is False
        assert contender_task.done() is False

        allow_transaction_to_finish.set()
        response = await asyncio.wait_for(edit_task, timeout=1)
        assert response.status_code == 200
        admitted_stream_id = response.json()["stream_id"]
        await asyncio.wait_for(contender_task, timeout=1)
        assert contender_entered.is_set()

        job = manager.get_job(admitted_stream_id)
        assert job is not None and job.task is not None
        await asyncio.wait_for(job.task, timeout=1)
        async with session_factory() as db:
            part = await db.get(Part, "atomic-edit-part")
        assert part is not None and part.data["text"] == "rewritten"
    finally:
        allow_transaction_to_finish.set()
        if not edit_task.done():
            edit_task.cancel()
        if contender_task is not None and not contender_task.done():
            contender_task.cancel()
        await asyncio.gather(
            edit_task,
            *(() if contender_task is None else (contender_task,)),
            return_exceptions=True,
        )
        for job in tuple(manager.active_jobs()):
            if job.task is not None and not job.task.done():
                job.task.cancel()
                await asyncio.gather(job.task, return_exceptions=True)
            job.complete()
            manager.remove_job(job.stream_id)
        if admitted_stream_id is not None:
            completed = manager.get_job(admitted_stream_id)
            if completed is not None:
                manager.remove_job(admitted_stream_id)
