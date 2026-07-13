from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.session import Session
from app.session.input_queue import (
    block_interrupted_inputs,
    cancel_session_input,
    claim_next_session_input,
    enqueue_session_input,
    finish_session_input,
    list_session_inputs,
    update_queued_session_input,
)


@pytest.fixture
async def queue_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="test", version="0.8.0"))
    try:
        yield factory
    finally:
        await engine.dispose()


async def _enqueue(db, request_id: str, mode: str = "queue"):
    return await enqueue_session_input(
        db,
        session_id="session",
        client_request_id=request_id,
        mode=mode,
        text=request_id,
        attachments=[],
        model_id="model",
        provider_id="provider",
        agent="build",
        language="zh",
        workspace=None,
        reasoning=False,
        permission_presets=None,
        permission_rules=None,
        target_stream_id="stream" if mode == "steer" else None,
    )


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_fifo(queue_db) -> None:
    async with queue_db() as db:
        async with db.begin():
            first, first_created = await _enqueue(db, "request-1")
            duplicate, duplicate_created = await _enqueue(db, "request-1")
            second, _ = await _enqueue(db, "request-2")
    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert second.position > first.position

    async with queue_db() as db:
        async with db.begin():
            claimed = await claim_next_session_input(db, "session", mode="queue")
            assert claimed is not None and claimed.id == first.id
            await finish_session_input(db, claimed.id, status="consumed")
        async with db.begin():
            claimed = await claim_next_session_input(db, "session", mode="queue")
            assert claimed is not None and claimed.id == second.id


@pytest.mark.asyncio
async def test_cancel_and_restart_block_are_safe(queue_db) -> None:
    async with queue_db() as db:
        async with db.begin():
            queued, _ = await _enqueue(db, "queued")
            applying, _ = await _enqueue(db, "applying")
            claimed = await claim_next_session_input(db, "session", mode="queue")
            assert claimed is not None and claimed.id == queued.id
            assert await cancel_session_input(db, "session", applying.id) is True
        async with db.begin():
            assert await block_interrupted_inputs(db) == 1

    async with queue_db() as db:
        items = await list_session_inputs(db, "session")
        assert [(item.id, item.status) for item in items] == [(queued.id, "blocked")]


@pytest.mark.asyncio
async def test_queued_inputs_can_be_reordered_and_promoted_to_steer(queue_db) -> None:
    async with queue_db() as db:
        async with db.begin():
            first, _ = await _enqueue(db, "request-first")
            second, _ = await _enqueue(db, "request-second")
            third, _ = await _enqueue(db, "request-third")
            moved = await update_queued_session_input(
                db,
                "session",
                first.id,
                position=3,
            )
            assert moved is not None
            steered = await update_queued_session_input(
                db,
                "session",
                first.id,
                mode="steer",
                target_stream_id="stream-current",
            )
            assert steered is not None

    async with queue_db() as db:
        items = await list_session_inputs(db, "session")
        assert [item.id for item in items] == [second.id, third.id, first.id]
        assert items[2].mode == "steer"
        assert items[2].target_stream_id == "stream-current"
