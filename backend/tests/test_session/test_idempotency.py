from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.idempotency_record import IdempotencyRecord
from app.session.idempotency import interrupt_inflight_idempotency_records


@pytest.mark.asyncio
async def test_restart_marks_only_ambiguous_requests_interrupted(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'requests.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        async with db.begin():
            db.add_all(
                [
                    IdempotencyRecord(
                        scope="chat.prompt",
                        request_key="accepted-key",
                        request_hash="a",
                        status="accepted",
                        response={},
                    ),
                    IdempotencyRecord(
                        scope="chat.prompt",
                        request_key="completed-key",
                        request_hash="b",
                        status="completed",
                        response={},
                    ),
                ]
            )
    async with factory() as db:
        async with db.begin():
            assert await interrupt_inflight_idempotency_records(db) == 1
    async with factory() as db:
        rows = {
            row.request_key: row.status
            for row in (await db.execute(select(IdempotencyRecord))).scalars()
        }
    assert rows == {"accepted-key": "interrupted", "completed-key": "completed"}
    await engine.dispose()
