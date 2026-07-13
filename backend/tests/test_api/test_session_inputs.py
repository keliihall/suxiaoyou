"""HTTP contract tests for follow-up queue and steer inputs."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.dependencies import set_stream_manager
from app.models.session import Session
from app.models.session_input import SessionInput
from app.streaming.events import INPUT_QUEUED
from app.streaming.manager import StreamManager


@pytest.fixture
def stream_manager() -> StreamManager:
    manager = StreamManager()
    set_stream_manager(manager)
    return manager


async def _create_session(session_factory, session_id: str = "session-1") -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id=session_id, directory=".", title="Queue test"))


def _payload(**overrides):
    payload = {
        "session_id": "session-1",
        "client_request_id": "client-request-1",
        "mode": "queue",
        "text": "Follow up",
        "model": "model-1",
        "provider_id": "provider-1",
        "agent": "build",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_queue_input_post_get_delete_and_idempotency(
    app_client,
    session_factory,
    stream_manager: StreamManager,
) -> None:
    await _create_session(session_factory)
    job = stream_manager.create_job("stream-1", "session-1")

    created = await app_client.post(
        "/api/chat/inputs",
        json=_payload(),
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["session_id"] == "session-1"
    assert body["client_request_id"] == "client-request-1"
    assert body["status"] == "queued"
    assert body["position"] == 1
    assert job.events[-1].event == INPUT_QUEUED
    assert job.events[-1].data["input_id"] == body["id"]
    async with session_factory() as db:
        stored = await db.get(SessionInput, body["id"])
    assert stored is not None and stored.language == "en"
    assert job.language == "zh"

    # A retry after a lost HTTP response resolves to the original durable row,
    # even if the in-memory stream already completed in the meantime.
    job.complete()
    duplicate = await app_client.post("/api/chat/inputs", json=_payload())
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == body["id"]
    assert duplicate.json()["text"] == "Follow up"
    assert [event.event for event in job.events].count(INPUT_QUEUED) == 1

    conflict = await app_client.post(
        "/api/chat/inputs",
        json=_payload(text="A changed request must not be silently discarded"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    listed = await app_client.get("/api/chat/inputs/session-1")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]

    cancelled = await app_client.delete(
        f"/api/chat/inputs/session-1/{body['id']}"
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {"status": "cancelled", "input_id": body["id"]}

    listed_after_cancel = await app_client.get("/api/chat/inputs/session-1")
    assert listed_after_cancel.status_code == 200
    assert listed_after_cancel.json() == []

    repeated_cancel = await app_client.delete(
        f"/api/chat/inputs/session-1/{body['id']}"
    )
    assert repeated_cancel.status_code == 409


@pytest.mark.asyncio
async def test_queue_input_requires_an_active_session_job(
    app_client,
    session_factory,
    stream_manager: StreamManager,
) -> None:
    del stream_manager
    await _create_session(session_factory)

    response = await app_client.post("/api/chat/inputs", json=_payload())

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_idle"


@pytest.mark.asyncio
async def test_queue_input_rejects_a_job_that_closed_input_admission(
    app_client,
    session_factory,
    stream_manager: StreamManager,
) -> None:
    await _create_session(session_factory)
    job = stream_manager.create_job("stream-finalizing", "session-1")
    job.close_session_input_admission()

    response = await app_client.post("/api/chat/inputs", json=_payload())

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_idle"


@pytest.mark.asyncio
async def test_prompt_rejects_a_second_active_job_for_the_same_session(
    app_client,
    stream_manager: StreamManager,
) -> None:
    active = stream_manager.create_job("stream-active", "session-1")

    response = await app_client.post(
        "/api/chat/prompt",
        json={"session_id": "session-1", "text": "Start another task"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "session_busy",
        "message": "This conversation already has a running task.",
        "session_id": "session-1",
        "active_stream_id": active.stream_id,
    }


@pytest.mark.asyncio
async def test_steer_is_bound_to_the_current_active_stream(
    app_client,
    session_factory,
    stream_manager: StreamManager,
) -> None:
    await _create_session(session_factory)
    active = stream_manager.create_job("stream-current", "session-1")

    response = await app_client.post(
        "/api/chat/inputs",
        json=_payload(mode="steer", client_request_id="client-steer-1"),
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "steer"
    assert response.json()["target_stream_id"] == active.stream_id


@pytest.mark.asyncio
async def test_queued_inputs_can_be_reordered_and_steered_before_execution(
    app_client,
    session_factory,
    stream_manager: StreamManager,
) -> None:
    await _create_session(session_factory)
    active = stream_manager.create_job("stream-current", "session-1")
    first = await app_client.post(
        "/api/chat/inputs",
        json=_payload(client_request_id="client-queue-first", text="First"),
    )
    second = await app_client.post(
        "/api/chat/inputs",
        json=_payload(client_request_id="client-queue-second", text="Second"),
    )

    moved = await app_client.patch(
        f"/api/chat/inputs/session-1/{second.json()['id']}",
        json={"position": 1},
    )
    assert moved.status_code == 200
    listed = await app_client.get("/api/chat/inputs/session-1")
    assert [item["text"] for item in listed.json()] == ["Second", "First"]

    steered = await app_client.patch(
        f"/api/chat/inputs/session-1/{first.json()['id']}",
        json={"mode": "steer"},
    )
    assert steered.status_code == 200
    assert steered.json()["mode"] == "steer"
    assert steered.json()["target_stream_id"] == active.stream_id


@pytest.mark.asyncio
async def test_folderless_queue_snapshots_attachment_despite_stale_workspace(
    app_client,
    session_factory,
    stream_manager: StreamManager,
    tmp_path: Path,
    monkeypatch,
) -> None:
    await _create_session(session_factory)
    stream_manager.create_job("stream-current", "session-1")
    source = tmp_path / "Recorder" / "meeting.m4a"
    source.parent.mkdir()
    source.write_bytes(b"audio")
    managed_root = tmp_path / "managed"
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(managed_root))

    response = await app_client.post(
        "/api/chat/inputs",
        json=_payload(
            client_request_id="client-attachment-1",
            workspace=str(tmp_path / "stale-project"),
            attachments=[
                {
                    "file_id": "recording-1",
                    "name": "meeting.m4a",
                    "path": str(source),
                    "size": len(b"audio"),
                    "mime_type": "audio/mp4",
                    "source": "referenced",
                }
            ],
        ),
    )

    assert response.status_code == 200
    [attachment] = response.json()["attachments"]
    copied = Path(attachment["path"])
    assert copied.parent == managed_root / "session-1" / "inputs"
    assert copied.read_bytes() == b"audio"
