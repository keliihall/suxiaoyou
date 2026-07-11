"""Structured acknowledgement contract for POST /api/chat/respond."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select

from app.api import chat as chat_api
from app.dependencies import set_stream_manager
from app.models.idempotency_record import IdempotencyRecord
from app.session.processor import _ask_permission
from app.streaming.events import (
    PERMISSION_REQUEST,
    PERMISSION_RESOLVED,
    PLAN_REVIEW_RESOLVED,
    QUESTION_RESOLVED,
    SSEEvent,
    TOOL_START,
)
from app.streaming.manager import StreamManager


@pytest.fixture
def stream_manager() -> StreamManager:
    manager = StreamManager()
    set_stream_manager(manager)
    return manager


@pytest.mark.asyncio
async def test_respond_reports_missing_unknown_and_expired_calls(
    app_client,
    stream_manager: StreamManager,
):
    missing = await app_client.post(
        "/api/chat/respond",
        json={"stream_id": "missing", "call_id": "call-1", "response": "yes"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "job_not_found"

    job = stream_manager.create_job("stream-1", "session-1")
    unknown = await app_client.post(
        "/api/chat/respond",
        json={"stream_id": job.stream_id, "call_id": "unknown", "response": "yes"},
    )
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "not_pending"

    job.register_response_request(
        "expired",
        prompt_type="question",
        timeout=0.0,
        tool_call_id="expired",
        tool="question",
    )
    expired = await app_client.post(
        "/api/chat/respond",
        json={"stream_id": job.stream_id, "call_id": "expired", "response": "yes"},
    )
    assert expired.status_code == 409
    assert expired.json()["detail"]["code"] == "expired"


@pytest.mark.asyncio
async def test_respond_is_idempotent_and_emits_correlated_resolution(
    app_client,
    session_factory,
    stream_manager: StreamManager,
):
    job = stream_manager.create_job("stream-1", "session-1")
    job.register_response_request(
        "permission-1",
        prompt_type="permission",
        timeout=300.0,
        tool_call_id="tool-1",
        tool="bash",
    )
    payload = {
        "stream_id": job.stream_id,
        "call_id": "permission-1",
        "response": {"allowed": True, "remember": False},
    }

    accepted = await app_client.post("/api/chat/respond", json=payload)
    assert accepted.status_code == 200
    assert accepted.json() == {
        "status": "accepted",
        "call_id": "permission-1",
        "tool_call_id": "tool-1",
        "tool": "bash",
        "prompt_type": "permission",
        "decision": "allowed",
        "source": "local",
        "idempotent": False,
    }
    event = job.events[-1]
    assert event.event == PERMISSION_RESOLVED
    assert event.data["decision"] == "allowed"
    assert event.data["source"] == "local"
    assert event.data["tool_call_id"] == "tool-1"

    async with session_factory() as db:
        durable = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope == "chat.respond:stream-1",
                    IdempotencyRecord.request_key == "permission-1",
                )
            )
        ).scalar_one()
    assert durable.status == "resolved"
    assert durable.response["submitted_response"] == payload["response"]
    assert durable.response["decision"] == "allowed"
    assert durable.response["source"] == "local"
    assert datetime.fromisoformat(durable.response["resolved_at"]).tzinfo is not None

    # Simulate a process restart/cleanup that lost the in-memory job.  The
    # durable response still makes an identical retry safe and a conflicting
    # retry visible.
    stream_manager.remove_job(job.stream_id)
    duplicate = await app_client.post("/api/chat/respond", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "already_resolved"
    assert duplicate.json()["idempotent"] is True

    conflict = await app_client.post(
        "/api/chat/respond",
        json={**payload, "response": {"allowed": False, "remember": False}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "response_conflict"
    assert conflict.json()["detail"]["existing_decision"] == "allowed"
    assert conflict.json()["detail"]["resolved_at"] == durable.response["resolved_at"]


@pytest.mark.asyncio
async def test_response_commit_precedes_waiter_wakeup(
    app_client,
    session_factory,
    stream_manager: StreamManager,
    monkeypatch,
) -> None:
    job = stream_manager.create_job("stream-durable-order", "session-durable-order")
    prompt = job.register_response_request(
        "question-durable-order",
        prompt_type="question",
        timeout=300.0,
        tool_call_id="question-tool-order",
        tool="question",
    )
    committed_while_waiting = asyncio.Event()
    original_persist = chat_api._persist_interaction_resolution

    async def asserting_persist(factory, record) -> str:
        record_id = await original_persist(factory, record)
        # This hook runs after the DB transaction committed but before the
        # route can apply the response to the in-memory Future.
        assert not prompt.future.done()
        committed_while_waiting.set()
        return record_id

    monkeypatch.setattr(
        chat_api,
        "_persist_interaction_resolution",
        asserting_persist,
    )
    response = await app_client.post(
        "/api/chat/respond",
        json={
            "stream_id": job.stream_id,
            "call_id": prompt.call_id,
            "response": "Proceed",
        },
    )

    assert response.status_code == 200
    assert committed_while_waiting.is_set()
    assert prompt.future.done()
    assert prompt.future.result() == "Proceed"
    async with session_factory() as db:
        durable = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope
                    == "chat.respond:stream-durable-order"
                )
            )
        ).scalar_one()
    assert durable.response["call_id"] == prompt.call_id


@pytest.mark.asyncio
async def test_response_commit_wins_over_timeout_in_commit_wake_window(
    app_client,
    stream_manager: StreamManager,
    monkeypatch,
) -> None:
    job = stream_manager.create_job("stream-timeout-race", "session-timeout-race")
    prompt = job.register_response_request(
        "question-timeout-race",
        prompt_type="question",
        timeout=300.0,
        tool_call_id="question-tool-timeout-race",
        tool="question",
    )

    class ObservableLock:
        """Expose when the timed-out waiter blocks behind the DB commit."""

        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self.contended = asyncio.Event()

        async def __aenter__(self):
            if self._lock.locked():
                self.contended.set()
            await self._lock.acquire()
            return self

        async def __aexit__(self, *_exc_info) -> None:
            self._lock.release()

    resolution_lock = ObservableLock()
    job.response_resolution_lock = resolution_lock
    persist_entered = asyncio.Event()
    release_persist = asyncio.Event()
    original_persist = chat_api._persist_interaction_resolution

    async def gated_persist(factory, record) -> str:
        persist_entered.set()
        await release_persist.wait()
        return await original_persist(factory, record)

    monkeypatch.setattr(
        chat_api,
        "_persist_interaction_resolution",
        gated_persist,
    )
    submit = asyncio.create_task(
        app_client.post(
            "/api/chat/respond",
            json={
                "stream_id": job.stream_id,
                "call_id": prompt.call_id,
                "response": "Accepted before timeout",
            },
        )
    )
    waiter: asyncio.Task[str] | None = None
    try:
        # Start the submitter first so it deterministically owns the response
        # lock before the short waiter timeout begins.  The wider deadline is
        # for overloaded shared CI runners, not product timing semantics.
        await asyncio.wait_for(persist_entered.wait(), timeout=10)
        waiter = asyncio.create_task(
            job.wait_for_response(prompt.call_id, timeout=0.01)
        )
        await asyncio.wait_for(resolution_lock.contended.wait(), timeout=10)
        assert not waiter.done()

        release_persist.set()
        response = await asyncio.wait_for(submit, timeout=10)

        assert response.status_code == 200
        assert (
            await asyncio.wait_for(waiter, timeout=10)
            == "Accepted before timeout"
        )
    finally:
        release_persist.set()
        tasks = [task for task in (submit, waiter) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_deleting_session_removes_only_its_durable_interaction_responses(
    app_client,
    session_factory,
    stream_manager: StreamManager,
) -> None:
    first_session = (
        await app_client.post("/api/sessions", json={"title": "Delete me"})
    ).json()["id"]
    second_session = (
        await app_client.post("/api/sessions", json={"title": "Keep me"})
    ).json()["id"]
    payloads = []
    for index, session_id in enumerate((first_session, second_session), start=1):
        job = stream_manager.create_job(f"stream-delete-{index}", session_id)
        job.register_response_request(
            f"question-delete-{index}",
            prompt_type="question",
            timeout=300.0,
            tool_call_id=f"tool-delete-{index}",
            tool="question",
        )
        payload = {
            "stream_id": job.stream_id,
            "call_id": f"question-delete-{index}",
            "response": f"private response {index}",
        }
        payloads.append(payload)
        accepted = await app_client.post("/api/chat/respond", json=payload)
        assert accepted.status_code == 200

    deleted = await app_client.delete(f"/api/sessions/{first_session}")
    assert deleted.status_code == 200

    async with session_factory() as db:
        records = list(
            (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope.like("chat.respond:%")
                    )
                )
            ).scalars()
        )
    assert len(records) == 1
    assert records[0].response["session_id"] == second_session
    assert records[0].response["submitted_response"] == "private response 2"

    # The unrelated session keeps its restart/lost-response idempotency.
    stream_manager.remove_job(payloads[1]["stream_id"])
    replay = await app_client.post("/api/chat/respond", json=payloads[1])
    assert replay.status_code == 200
    assert replay.json()["status"] == "already_resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_type", "response", "event_type", "decision"),
    [
        ("question", "Use the first option", QUESTION_RESOLVED, "answered"),
        (
            "plan",
            '{"action":"accept","mode":"ask"}',
            PLAN_REVIEW_RESOLVED,
            "accept",
        ),
    ],
)
async def test_resolved_event_matches_prompt_type(
    app_client,
    stream_manager: StreamManager,
    prompt_type: str,
    response: str,
    event_type: str,
    decision: str,
):
    job = stream_manager.create_job(f"stream-{prompt_type}", "session-1")
    job.register_response_request(
        f"{prompt_type}-1",
        prompt_type=prompt_type,
        timeout=300.0,
        tool_call_id=f"{prompt_type}-1",
        tool="question" if prompt_type == "question" else "submit_plan",
    )
    result = await app_client.post(
        "/api/chat/respond",
        json={
            "stream_id": job.stream_id,
            "call_id": f"{prompt_type}-1",
            "response": response,
        },
    )
    assert result.status_code == 200
    assert result.json()["decision"] == decision
    assert job.events[-1].event == event_type


@pytest.mark.asyncio
async def test_permission_request_response_acceptance_precedes_tool_start(
    app_client,
    stream_manager: StreamManager,
):
    """Exercise request -> HTTP acknowledgement -> guarded tool continuation."""
    job = stream_manager.create_job("stream-flow", "session-flow")
    job.interactive = True

    async def run_guarded_tool() -> None:
        decision = await _ask_permission(
            job,
            call_id="tool-call-1",
            tool_name="bash",
            tool_args={"command": "echo safe"},
        )
        if decision["allowed"]:
            job.publish(SSEEvent(TOOL_START, {
                "call_id": "tool-call-1",
                "tool": "bash",
                "arguments": {"command": "echo safe"},
            }))

    task = asyncio.create_task(run_guarded_tool())
    for _ in range(100):
        if job.events:
            break
        await asyncio.sleep(0)

    request_event = job.events[0]
    assert request_event.event == PERMISSION_REQUEST
    response = await app_client.post(
        "/api/chat/respond",
        json={
            "stream_id": job.stream_id,
            "call_id": request_event.data["call_id"],
            "response": {"allowed": True, "remember": False},
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    await asyncio.wait_for(task, timeout=1.0)
    event_types = [event.event for event in job.events]
    assert event_types == [PERMISSION_REQUEST, PERMISSION_RESOLVED, TOOL_START]
