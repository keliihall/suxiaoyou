from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_input import SessionInput
from app.session.upload_gc import collect_orphan_uploads


@pytest.mark.asyncio
async def test_gc_keeps_shared_reference_and_collects_only_old_orphan(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    shared = upload_dir / "shared.bin"
    orphan = upload_dir / "orphan.bin"
    recent = upload_dir / "recent.bin"
    for path in (shared, orphan, recent):
        path.write_bytes(path.name.encode())
    os.utime(shared, (10, 10))
    os.utime(orphan, (10, 10))
    os.utime(recent, (990, 990))

    async with factory() as db:
        async with db.begin():
            session = Session(id="session", directory=".", title="test", version="0.7.3")
            message = Message(id="message", session_id="session", data={"role": "user"})
            part = Part(
                id="part",
                message_id="message",
                session_id="session",
                data={
                    "type": "file",
                    "source": "uploaded",
                    "path": str(shared),
                },
            )
            db.add_all([session, message, part])

    deleted = await collect_orphan_uploads(
        factory,
        upload_dir,
        min_age_seconds=100,
        now=1_000,
    )

    assert deleted == [orphan.resolve()]
    assert shared.exists()
    assert recent.exists()
    assert not orphan.exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_gc_collects_shared_file_only_after_final_reference_is_committed(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    shared = upload_dir / "shared.bin"
    shared.write_bytes(b"shared")
    os.utime(shared, (10, 10))

    async with factory() as db:
        async with db.begin():
            for index in range(2):
                session_id = f"session-{index}"
                message_id = f"message-{index}"
                db.add(Session(id=session_id, directory=".", title="test", version="0.7.3"))
                db.add(Message(id=message_id, session_id=session_id, data={"role": "user"}))
                db.add(
                    Part(
                        id=f"part-{index}",
                        message_id=message_id,
                        session_id=session_id,
                        data={"type": "file", "source": "uploaded", "path": str(shared)},
                    )
                )

    async with factory() as db:
        async with db.begin():
            await db.execute(delete(Part).where(Part.session_id == "session-0"))
    assert await collect_orphan_uploads(factory, upload_dir, min_age_seconds=0) == []
    assert shared.exists()

    async with factory() as db:
        async with db.begin():
            await db.execute(delete(Part).where(Part.session_id == "session-1"))
    assert await collect_orphan_uploads(factory, upload_dir, min_age_seconds=0) == [
        shared.resolve()
    ]
    assert not shared.exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_gc_preserves_upload_referenced_only_by_durable_queue(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    queued_upload = upload_dir / "queued.m4a"
    terminal_upload = upload_dir / "cancelled.m4a"
    queued_upload.write_bytes(b"queued")
    terminal_upload.write_bytes(b"cancelled")
    os.utime(queued_upload, (10, 10))
    os.utime(terminal_upload, (10, 10))

    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory="/project", title="test"))
            db.add_all(
                [
                    SessionInput(
                        id="queued-input",
                        session_id="session",
                        client_request_id="queued-request",
                        status="blocked",
                        position=1,
                        text="transcribe",
                        attachments=[
                            {
                                "type": "file",
                                "source": "uploaded",
                                "path": str(queued_upload),
                            }
                        ],
                    ),
                    SessionInput(
                        id="cancelled-input",
                        session_id="session",
                        client_request_id="cancelled-request",
                        status="cancelled",
                        position=2,
                        text="cancelled",
                        attachments=[
                            {
                                "type": "file",
                                "source": "uploaded",
                                "path": str(terminal_upload),
                            }
                        ],
                    ),
                ]
            )

    deleted = await collect_orphan_uploads(
        factory,
        upload_dir,
        min_age_seconds=0,
        now=1_000,
    )

    assert deleted == [terminal_upload.resolve()]
    assert queued_upload.exists()
    assert not terminal_upload.exists()
    await engine.dispose()
