from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.message import Message, Part
from app.models.session import Session
from app.session.recovery import interrupt_inflight_tool_parts


@pytest.mark.asyncio
async def test_startup_recovery_terminates_only_inflight_tool_cards(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Recovery"))
            db.add(Message(id="message", session_id="session", data={"role": "assistant"}))
            for status in ("pending", "running", "completed", "error"):
                db.add(
                    Part(
                        id=f"part-{status}",
                        message_id="message",
                        session_id="session",
                        data={
                            "type": "tool",
                            "tool": "bash",
                            "call_id": f"call-{status}",
                            "state": {
                                "status": status,
                                "input": {"command": "side-effect"},
                                "output": f"old-{status}",
                            },
                        },
                    )
                )
            db.add(
                Part(
                    id="part-text",
                    message_id="message",
                    session_id="session",
                    data={"type": "text", "text": "running"},
                )
            )

    async with factory() as db:
        async with db.begin():
            assert await interrupt_inflight_tool_parts(db) == 2

    async with factory() as db:
        pending = await db.get(Part, "part-pending")
        running = await db.get(Part, "part-running")
        completed = await db.get(Part, "part-completed")
        error = await db.get(Part, "part-error")
        text = await db.get(Part, "part-text")

    for interrupted in (pending, running):
        assert interrupted is not None
        assert interrupted.data["state"]["status"] == "error"
        assert interrupted.data["state"]["error_type"] == "interrupted"
        assert interrupted.data["state"]["interrupted"] is True
        assert "restarted" in interrupted.data["state"]["output"]
        assert interrupted.data["state"]["input"] == {"command": "side-effect"}
    assert completed is not None and completed.data["state"]["output"] == "old-completed"
    assert error is not None and error.data["state"]["output"] == "old-error"
    assert text is not None and text.data == {"type": "text", "text": "running"}

    async with factory() as db:
        async with db.begin():
            assert await interrupt_inflight_tool_parts(db) == 0
    await engine.dispose()
