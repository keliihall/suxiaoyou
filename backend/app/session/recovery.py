"""Persistent startup recovery for generation state left mid-flight."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Part


_INTERRUPTED_TOOL_MESSAGE = "Application restarted before this tool completed."


async def interrupt_inflight_tool_parts(db: AsyncSession) -> int:
    """Turn stale tool cards into terminal errors after a process restart.

    Tool execution is not replayable: it may already have changed files or an
    external system before the process exited.  Leaving its durable card in a
    ``running``/``pending`` state makes the UI wait forever, while replaying it
    could duplicate side effects.  Marking it interrupted is both terminal and
    honest about the unknown outcome.
    """

    result = await db.execute(
        select(Part).where(
            Part.data["type"].as_string() == "tool",
            Part.data["state"]["status"].as_string().in_(("pending", "running")),
        )
    )
    parts = list(result.scalars().all())
    for part in parts:
        data = dict(part.data or {})
        state = dict(data.get("state") or {})
        state.update(
            {
                "status": "error",
                "output": _INTERRUPTED_TOOL_MESSAGE,
                "error_type": "interrupted",
                "interrupted": True,
            }
        )
        data["state"] = state
        part.data = data
    if parts:
        await db.flush()
    return len(parts)
