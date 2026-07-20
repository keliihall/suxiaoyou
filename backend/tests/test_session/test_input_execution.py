"""Execution semantics for durable queued and steer inputs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.models.base import Base
from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_input import SessionInput
from app.schemas.chat import PromptRequest
from app.session.input_queue import enqueue_session_input
from app.session.processor import run_generation
from app.session.prompt import SessionPrompt
from app.streaming.events import (
    AGENT_ERROR,
    DONE,
    INPUT_APPLIED,
    INPUT_FAILED,
    INPUT_STARTED,
    SSEEvent,
)
from app.streaming.manager import GenerationJob


@pytest.fixture
async def execution_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'execution.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Input execution"))
    try:
        yield factory
    finally:
        await engine.dispose()


async def _enqueue(
    factory,
    request_id: str,
    *,
    mode: str = "queue",
    text: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    target_stream_id: str | None = None,
    language: str = "zh",
) -> SessionInput:
    async with factory() as db:
        async with db.begin():
            item, _ = await enqueue_session_input(
                db,
                session_id="session",
                client_request_id=request_id,
                mode=mode,
                text=text if text is not None else request_id,
                attachments=attachments or [],
                model_id="model",
                provider_id="provider",
                agent="build",
                language=language,
                workspace=None,
                reasoning=False,
                permission_presets=None,
                permission_rules=None,
                target_stream_id=target_stream_id,
            )
    return item


@pytest.mark.asyncio
async def test_queued_inputs_run_fifo_on_one_stream_with_one_done(
    execution_db,
    monkeypatch,
) -> None:
    first = await _enqueue(
        execution_db, "queue-request-1", text="first queued", language="en"
    )
    second = await _enqueue(
        execution_db, "queue-request-2", text="second queued", language="zh"
    )
    job = GenerationJob("stream", "session")
    late_steer = await _enqueue(
        execution_db,
        "steer-request-late",
        mode="steer",
        text="late steer fallback",
        target_stream_id=job.stream_id,
        language="en",
    )

    class FakePrompt:
        requests: list[PromptRequest] = []

        def __init__(self, job, request, **kwargs):
            del kwargs
            self.job = job
            self.request = request
            self.total_cost = 1.0
            self.finish_reason = "stop"
            type(self).requests.append(request)

        async def run(self, *, publish_done: bool = True) -> None:
            assert publish_done is False

        def publish_done(self) -> None:
            self.job.publish(
                SSEEvent(
                    DONE,
                    {
                        "session_id": self.job.session_id,
                        "finish_reason": self.finish_reason,
                        "total_cost": self.total_cost,
                    },
                )
            )

    monkeypatch.setattr("app.session.prompt.SessionPrompt", FakePrompt)

    await run_generation(
        job,
        PromptRequest(session_id="session", text="initial"),
        session_factory=execution_db,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    assert [request.text for request in FakePrompt.requests] == [
        "initial",
        "first queued",
        "second queued",
        "late steer fallback",
    ]
    assert [request.language for request in FakePrompt.requests] == [
        "zh",
        "en",
        "zh",
        "en",
    ]
    started = [event for event in job.events if event.event == INPUT_STARTED]
    assert [event.data["mode"] for event in started] == ["queue", "queue", "steer"]
    assert [event.event for event in job.events].count(DONE) == 1
    assert job.events[-1].event == DONE
    assert job.events[-1].data["total_cost"] == 4.0
    assert job.completed is True

    async with execution_db() as db:
        rows = (
            await db.execute(
                select(SessionInput)
                .where(SessionInput.id.in_((first.id, second.id, late_steer.id)))
                .order_by(SessionInput.position)
            )
        ).scalars().all()
    assert [row.status for row in rows] == ["consumed", "consumed", "consumed"]
    assert [row.applied_stream_id for row in rows] == ["stream", "stream", "stream"]


@pytest.mark.asyncio
async def test_abort_blocks_without_claiming_the_next_queued_input(
    execution_db,
    monkeypatch,
) -> None:
    queued = await _enqueue(execution_db, "queue-after-stop")
    job = GenerationJob("stream", "session")

    class AbortingPrompt:
        def __init__(self, job, request, **kwargs):
            del request, kwargs
            self.job = job
            self.total_cost = 0.0
            self.finish_reason = "stop"

        async def run(self, *, publish_done: bool = True) -> None:
            assert publish_done is False
            self.job.abort()

        def publish_done(self) -> None:
            self.job.publish(SSEEvent(DONE, {"finish_reason": "aborted"}))

    monkeypatch.setattr("app.session.prompt.SessionPrompt", AbortingPrompt)

    await run_generation(
        job,
        PromptRequest(session_id="session", text="initial"),
        session_factory=execution_db,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    assert not any(event.event == INPUT_STARTED for event in job.events)
    async with execution_db() as db:
        stored = await db.get(SessionInput, queued.id)
    assert stored is not None
    assert stored.status == "blocked"
    assert "stopped" in (stored.error_message or "")


@pytest.mark.asyncio
async def test_agent_error_is_detected_after_replay_buffer_rollover(
    execution_db,
    monkeypatch,
) -> None:
    queued = await _enqueue(execution_db, "queue-after-long-error")
    job = GenerationJob("stream", "session")
    for index in range(job._MAX_EVENT_BUFFER):
        job.publish(SSEEvent("noise", {"index": index}))

    class ErrorPrompt:
        def __init__(self, job, request, **kwargs):
            del request, kwargs
            self.job = job
            self.total_cost = 0.0
            self.finish_reason = "error"

        async def run(self, *, publish_done: bool = True) -> None:
            assert publish_done is False
            self.job.publish(
                SSEEvent(AGENT_ERROR, {"error_message": "provider failed"})
            )

        def publish_done(self) -> None:
            self.job.publish(SSEEvent(DONE, {"finish_reason": self.finish_reason}))

    monkeypatch.setattr("app.session.prompt.SessionPrompt", ErrorPrompt)

    await run_generation(
        job,
        PromptRequest(session_id="session", text="initial"),
        session_factory=execution_db,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    assert not any(event.event == INPUT_STARTED for event in job.events)
    assert any(event.event == AGENT_ERROR for event in job.events)
    async with execution_db() as db:
        stored = await db.get(SessionInput, queued.id)
    assert stored is not None and stored.status == "blocked"
    assert "failed" in (stored.error_message or "")


@pytest.mark.asyncio
async def test_task_cancellation_blocks_the_claimed_queued_input(
    execution_db,
    monkeypatch,
) -> None:
    queued = await _enqueue(execution_db, "queue-cancelled-mid-run")
    job = GenerationJob("stream", "session")

    class CancellingPrompt:
        calls = 0

        def __init__(self, job, request, **kwargs):
            del request, kwargs
            self.job = job
            self.total_cost = 0.0
            self.finish_reason = "stop"

        async def run(self, *, publish_done: bool = True) -> None:
            assert publish_done is False
            type(self).calls += 1
            if type(self).calls == 2:
                raise asyncio.CancelledError

        def publish_done(self) -> None:
            self.job.publish(SSEEvent(DONE, {"finish_reason": self.finish_reason}))

    monkeypatch.setattr("app.session.prompt.SessionPrompt", CancellingPrompt)

    with pytest.raises(asyncio.CancelledError):
        await run_generation(
            job,
            PromptRequest(session_id="session", text="initial"),
            session_factory=execution_db,
            provider_registry=object(),
            agent_registry=object(),
            tool_registry=object(),
        )

    async with execution_db() as db:
        stored = await db.get(SessionInput, queued.id)
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.applied_stream_id == job.stream_id
    assert "cancelled" in (stored.error_message or "")
    assert job.completed is True


def _steer_prompt(job: GenerationJob, execution_db) -> SessionPrompt:
    prompt = object.__new__(SessionPrompt)
    prompt.job = job
    prompt.request = PromptRequest(session_id="session", text="initial", language="zh")
    prompt.session_factory = execution_db
    prompt.system_prompt_parts = "prompt-zh"  # type: ignore[assignment]
    prompt._build_system_prompt_parts = (  # type: ignore[method-assign]
        lambda: f"prompt-{prompt.request.language}"
    )
    return prompt


@pytest.mark.asyncio
async def test_steer_safe_boundary_atomically_persists_message_and_parts(
    execution_db,
) -> None:
    job = GenerationJob("stream", "session")
    item = await _enqueue(
        execution_db,
        "steer-request-1",
        mode="steer",
        text="Please change direction",
        attachments=[{"type": "untrusted", "name": "notes.txt", "path": "/tmp/notes.txt"}],
        target_stream_id=job.stream_id,
        language="en",
    )

    prompt = _steer_prompt(job, execution_db)
    applied = await prompt._apply_pending_steers()

    assert applied == 1
    assert prompt.request.language == "en"
    assert job.language == "en"
    assert prompt.system_prompt_parts == "prompt-en"
    assert [event.event for event in job.events] == [INPUT_APPLIED]
    async with execution_db() as db:
        stored = await db.get(SessionInput, item.id)
        message = (
            await db.execute(
                select(Message)
                .where(Message.session_id == "session")
                .options(selectinload(Message.parts))
            )
        ).scalar_one()
    assert stored is not None
    assert stored.status == "consumed"
    assert stored.applied_stream_id == job.stream_id
    assert message.data["session_input_id"] == item.id
    assert [part.data["type"] for part in message.parts] == ["text", "file"]
    assert message.parts[1].data["name"] == "notes.txt"


@pytest.mark.asyncio
async def test_failed_steer_rolls_back_the_entire_message(
    execution_db,
    monkeypatch,
) -> None:
    job = GenerationJob("stream", "session")
    item = await _enqueue(
        execution_db,
        "steer-request-failure",
        mode="steer",
        text="This text part is written first",
        attachments=[{"name": "broken.txt", "path": "/tmp/broken.txt"}],
        target_stream_id=job.stream_id,
    )

    from app.session import prompt as prompt_module

    original_create_part = prompt_module.create_part
    calls = 0

    async def fail_after_first_part(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("attachment write failed")
        return await original_create_part(*args, **kwargs)

    monkeypatch.setattr(prompt_module, "create_part", fail_after_first_part)

    applied = await _steer_prompt(job, execution_db)._apply_pending_steers()

    assert applied == 0
    assert [event.event for event in job.events] == [INPUT_FAILED]
    async with execution_db() as db:
        stored = await db.get(SessionInput, item.id)
        messages = list((await db.execute(select(Message))).scalars().all())
        parts = list((await db.execute(select(Part))).scalars().all())
    assert stored is not None
    assert stored.status == "failed"
    assert "attachment write failed" in (stored.error_message or "")
    assert messages == []
    assert parts == []
