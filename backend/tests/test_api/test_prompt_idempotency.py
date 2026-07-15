"""Durable idempotency contract for starting a generation."""

from __future__ import annotations

import asyncio
import gc

import pytest
from sqlalchemy import select

from app import release_features
from app.api import chat as chat_api
from app.dependencies import set_stream_manager
from app.models.idempotency_record import IdempotencyRecord
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.chat import PromptRequest
from app.session.idempotency import canonical_request_hash
from app.streaming.manager import StreamManager


@pytest.mark.asyncio
async def test_unreleased_legacy_goal_row_does_not_block_ordinary_prompt(
    app_client,
    session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(release_features, "GOALS_RELEASED", False)
    manager = StreamManager()
    set_stream_manager(manager)
    calls = 0

    async def fake_generation(job, _request, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        job.complete()

    monkeypatch.setattr(chat_api, "run_generation", fake_generation)
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="legacy-goal-session", directory=".", title="Legacy"))
            db.add(
                SessionGoal(
                    id="legacy-hidden-goal",
                    session_id="legacy-goal-session",
                    objective="A hidden pre-release Goal row",
                    status="active",
                    run_state="idle",
                )
            )

    response = await app_client.post(
        "/api/chat/prompt",
        json={
            "client_request_id": "legacy-goal-ordinary-prompt",
            "session_id": "legacy-goal-session",
            "text": "Continue as an ordinary conversation",
        },
    )

    assert response.status_code == 200
    job = manager.get_job(response.json()["stream_id"])
    assert job is not None and job.task is not None
    await job.task
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_prompt_admission_finishes_commit_and_installs_worker(
    app_client,
    session_factory,
    monkeypatch,
) -> None:
    """A disconnected POST must not publish an accepted dead stream."""

    manager = StreamManager()
    set_stream_manager(manager)
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    generation_started = asyncio.Event()
    release_generation = asyncio.Event()
    generation_calls = 0

    original_persist = chat_api._persist_prompt_idempotency_record

    async def gated_persist(factory, record) -> str:
        commit_entered.set()
        await release_commit.wait()
        return await original_persist(factory, record)

    async def fake_generation(job, _request, **_kwargs) -> None:
        nonlocal generation_calls
        generation_calls += 1
        generation_started.set()
        await release_generation.wait()
        job.complete()

    monkeypatch.setattr(chat_api, "_persist_prompt_idempotency_record", gated_persist)
    monkeypatch.setattr(chat_api, "run_generation", fake_generation)
    payload = {
        "client_request_id": "prompt-request-cancelled-admission",
        "session_id": "cancelled-admission-session",
        "text": "Do not leave a dead stream",
    }

    request_task = asyncio.create_task(
        app_client.post("/api/chat/prompt", json=payload)
    )
    await asyncio.wait_for(commit_entered.wait(), timeout=1)
    request_task.cancel()
    await asyncio.sleep(0)

    # Request cancellation is held until admission has a durable outcome and
    # can install the worker paired with that outcome.
    assert not request_task.done()
    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    await asyncio.wait_for(generation_started.wait(), timeout=1)
    async with session_factory() as db:
        records = list((await db.execute(select(IdempotencyRecord))).scalars())
    assert len(records) == 1
    assert records[0].status == "accepted"
    stream_id = records[0].response["stream_id"]
    job = manager.get_job(stream_id)
    assert job is not None
    assert job.task is not None and not job.task.done()

    retry = await app_client.post("/api/chat/prompt", json=payload)
    assert retry.status_code == 200
    assert retry.json() == records[0].response
    assert generation_calls == 1

    release_generation.set()
    await asyncio.wait_for(job.task, timeout=1)


@pytest.mark.asyncio
async def test_lost_prompt_response_replays_original_session_and_stream(
    app_client,
    session_factory,
    monkeypatch,
) -> None:
    manager = StreamManager()
    set_stream_manager(manager)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def fake_generation(job, request, **_kwargs) -> None:
        calls.append((job.stream_id, request.text))
        started.set()
        await release.wait()
        job.complete()

    monkeypatch.setattr("app.api.chat.run_generation", fake_generation)
    payload = {
        "client_request_id": "prompt-request-0001",
        "session_id": None,
        "text": "Generate exactly once",
    }

    first = await app_client.post("/api/chat/prompt", json=payload)
    assert first.status_code == 200
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    job = manager.get_job(first.json()["stream_id"])
    assert job is not None and job.task is not None
    await asyncio.wait_for(job.task, timeout=1)

    # Simulate the client retrying after the original HTTP response was lost,
    # even though generation already completed in memory.
    replay = await app_client.post("/api/chat/prompt", json=payload)

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert calls == [(first.json()["stream_id"], "Generate exactly once")]
    async with session_factory() as db:
        records = list((await db.execute(select(IdempotencyRecord))).scalars())
    assert len(records) == 1
    assert records[0].response == first.json()


@pytest.mark.asyncio
async def test_prompt_request_key_reuse_with_different_payload_is_rejected(
    app_client,
    monkeypatch,
) -> None:
    manager = StreamManager()
    set_stream_manager(manager)
    release = asyncio.Event()

    async def fake_generation(job, _request, **_kwargs) -> None:
        await release.wait()
        job.complete()

    monkeypatch.setattr("app.api.chat.run_generation", fake_generation)
    original = {
        "client_request_id": "prompt-request-conflict",
        "text": "original",
    }
    first = await app_client.post("/api/chat/prompt", json=original)
    assert first.status_code == 200

    conflict = await app_client.post(
        "/api/chat/prompt",
        json={**original, "text": "different"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    release.set()
    job = manager.get_job(first.json()["stream_id"])
    assert job is not None and job.task is not None
    await asyncio.wait_for(job.task, timeout=1)


@pytest.mark.asyncio
async def test_interrupted_prompt_key_does_not_return_a_dead_stream(
    app_client,
    session_factory,
) -> None:
    manager = StreamManager()
    set_stream_manager(manager)
    payload = {
        "client_request_id": "prompt-request-interrupted",
        "session_id": "session-interrupted",
        "text": "May have partially executed",
    }
    request = PromptRequest(**payload)
    async with session_factory() as db:
        async with db.begin():
            db.add(
                IdempotencyRecord(
                    scope="chat.prompt",
                    request_key=payload["client_request_id"],
                    request_hash=canonical_request_hash(
                        request.model_dump(
                            mode="json",
                            exclude={"client_request_id"},
                        )
                    ),
                    status="interrupted",
                    response={
                        "stream_id": "dead-stream",
                        "session_id": "session-interrupted",
                    },
                )
            )

    replay = await app_client.post("/api/chat/prompt", json=payload)

    assert replay.status_code == 409
    assert replay.json()["detail"] == {
        "code": "idempotency_interrupted",
        "message": (
            "The previous request was interrupted before completion. "
            "Review the partial conversation, then send again to start a new task."
        ),
        "session_id": "session-interrupted",
        "stream_id": "dead-stream",
    }
    assert manager.get_job("dead-stream") is None


@pytest.mark.asyncio
async def test_semaphore_rejection_closes_generation_and_finalizes_ledger(
    app_client,
    session_factory,
    monkeypatch,
    recwarn,
) -> None:
    manager = StreamManager()
    set_stream_manager(manager)

    class RejectingSemaphore:
        async def acquire(self) -> None:
            raise asyncio.TimeoutError

        def release(self) -> None:  # pragma: no cover - must never be called
            raise AssertionError("unacquired semaphore was released")

    manager._semaphore = RejectingSemaphore()
    generation_started = False

    async def fake_generation(*_args, **_kwargs) -> None:
        nonlocal generation_started
        generation_started = True

    monkeypatch.setattr("app.api.chat.run_generation", fake_generation)
    payload = {
        "client_request_id": "prompt-request-overloaded",
        "session_id": "session-overloaded",
        "text": "Run when admitted",
    }

    response = await app_client.post("/api/chat/prompt", json=payload)
    assert response.status_code == 200
    job = manager.get_job(response.json()["stream_id"])
    assert job is not None and job.task is not None
    await asyncio.wait_for(job.task, timeout=1)
    gc.collect()

    assert generation_started is False
    assert job.completed is True
    assert manager.active_job_for_session("session-overloaded") is None
    assert any(event.event == "agent-error" for event in job.events)
    assert not [
        warning
        for warning in recwarn
        if "was never awaited" in str(warning.message)
    ]
    async with session_factory() as db:
        record = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.request_key == "prompt-request-overloaded"
                )
            )
        ).scalar_one()
    assert record.status == "failed"
    assert "server remained busy" in (record.error_message or "")

    # A lost HTTP response still converges on the completed error stream; it
    # never creates a second execution under the same key.
    replay = await app_client.post("/api/chat/prompt", json=payload)
    assert replay.status_code == 200
    assert replay.json() == response.json()
    assert generation_started is False


@pytest.mark.asyncio
async def test_semaphore_rejection_blocks_followups_accepted_while_waiting(
    app_client,
    session_factory,
    monkeypatch,
) -> None:
    manager = StreamManager()
    set_stream_manager(manager)
    waiting = asyncio.Event()
    reject = asyncio.Event()

    class ControlledRejectingSemaphore:
        async def acquire(self) -> None:
            waiting.set()
            await reject.wait()
            raise asyncio.TimeoutError

        def release(self) -> None:  # pragma: no cover - must never be called
            raise AssertionError("unacquired semaphore was released")

    manager._semaphore = ControlledRejectingSemaphore()

    async def generation_must_not_start(*_args, **_kwargs) -> None:
        raise AssertionError("generation should be closed before it starts")

    monkeypatch.setattr("app.api.chat.run_generation", generation_must_not_start)
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="existing-session", directory="/project", title="Existing"))

    prompt = await app_client.post(
        "/api/chat/prompt",
        json={
            "client_request_id": "prompt-request-waiting",
            "session_id": "existing-session",
            "text": "Long initial task",
        },
    )
    assert prompt.status_code == 200
    await asyncio.wait_for(waiting.wait(), timeout=1)

    queued = await app_client.post(
        "/api/chat/inputs",
        json={
            "session_id": "existing-session",
            "client_request_id": "followup-while-waiting",
            "mode": "queue",
            "text": "Run after the initial task",
        },
    )
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"

    reject.set()
    job = manager.get_job(prompt.json()["stream_id"])
    assert job is not None and job.task is not None
    await asyncio.wait_for(job.task, timeout=1)

    pending = await app_client.get("/api/chat/inputs/existing-session")
    assert pending.status_code == 200
    assert len(pending.json()) == 1
    assert pending.json()[0]["status"] == "blocked"
    assert "never started" in pending.json()[0]["error_message"]
    assert manager.active_job_for_session("existing-session") is None
