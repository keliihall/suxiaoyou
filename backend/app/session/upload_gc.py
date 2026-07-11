"""Reference-aware garbage collection for browser-uploaded files.

Physical files are never deleted inside a message/session transaction. Doing
so can lose data when another session references the deduplicated blob or when
the transaction later rolls back. Orphans are collected only after committed
database state has been inspected and a grace period has elapsed.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.message import Part
from app.models.session_input import SessionInput


logger = logging.getLogger(__name__)


async def referenced_upload_paths(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[Path]:
    async with session_factory() as db:
        result = await db.execute(select(Part))
        references: set[Path] = set()
        for part in result.scalars().all():
            data = part.data or {}
            if data.get("type") != "file" or data.get("source", "uploaded") != "uploaded":
                continue
            path_value = str(data.get("path") or "").strip()
            if path_value:
                references.add(Path(path_value).expanduser().resolve())

        # A project-bound follow-up can retain an uploaded blob in the durable
        # queue before a user-message Part exists.  In particular, interrupted
        # rows become ``blocked`` at startup and may be reviewed much later than
        # the orphan grace period.  Treat every runnable/reviewable queue row as
        # a live reference so startup GC cannot delete its input underneath it.
        queued = await db.execute(
            select(SessionInput).where(
                SessionInput.status.in_(("queued", "applying", "blocked"))
            )
        )
        for item in queued.scalars().all():
            for attachment in item.attachments or []:
                if attachment.get("source", "uploaded") != "uploaded":
                    continue
                path_value = str(attachment.get("path") or "").strip()
                if path_value:
                    references.add(Path(path_value).expanduser().resolve())
        return references


async def collect_orphan_uploads(
    session_factory: async_sessionmaker[AsyncSession],
    upload_dir: Path,
    *,
    min_age_seconds: float = 24 * 60 * 60,
    now: float | None = None,
) -> list[Path]:
    """Delete committed, unreferenced uploads older than the grace period."""
    if not upload_dir.is_dir():
        return []

    references = await referenced_upload_paths(session_factory)
    cutoff = (time.time() if now is None else now) - max(0, min_age_seconds)
    deleted: list[Path] = []
    for candidate in upload_dir.iterdir():
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in references:
            continue
        try:
            if candidate.stat().st_mtime > cutoff:
                continue
            candidate.unlink()
            deleted.append(resolved)
        except OSError:
            logger.warning("Failed to collect orphan upload: %s", candidate)

    if deleted:
        logger.info("Collected %d orphan upload(s)", len(deleted))
    return deleted
