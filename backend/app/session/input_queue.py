"""Transactional queue for follow-up and steer inputs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_input import SessionInput


InputMode = Literal["queue", "steer"]


async def enqueue_session_input(
    db: AsyncSession,
    *,
    session_id: str,
    client_request_id: str,
    mode: InputMode,
    text: str,
    attachments: list[dict[str, Any]],
    model_id: str | None,
    provider_id: str | None,
    agent: str,
    workspace: str | None,
    reasoning: bool | None,
    permission_presets: dict[str, bool] | None,
    permission_rules: list[dict[str, Any]] | None,
    target_stream_id: str | None,
) -> tuple[SessionInput, bool]:
    """Enqueue once per ``session_id + client_request_id``."""
    existing = (
        await db.execute(
            select(SessionInput).where(
                SessionInput.session_id == session_id,
                SessionInput.client_request_id == client_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    max_position = (
        await db.execute(
            select(func.max(SessionInput.position)).where(
                SessionInput.session_id == session_id,
                SessionInput.status.in_(("queued", "applying", "blocked")),
            )
        )
    ).scalar_one_or_none()
    item = SessionInput(
        session_id=session_id,
        client_request_id=client_request_id,
        mode=mode,
        status="queued",
        position=(max_position or 0) + 1,
        text=text,
        attachments=attachments,
        model_id=model_id,
        provider_id=provider_id,
        agent=agent,
        workspace=workspace,
        reasoning=reasoning,
        permission_presets=permission_presets,
        permission_rules=permission_rules,
        target_stream_id=target_stream_id,
    )
    db.add(item)
    try:
        await db.flush()
    except Exception:
        # A concurrent client may have inserted the same idempotency key after
        # our initial lookup. Let the caller's transaction roll back and retry
        # the lookup rather than manufacturing a second request.
        raise
    return item, True


async def list_session_inputs(
    db: AsyncSession,
    session_id: str,
    *,
    include_terminal: bool = False,
) -> list[SessionInput]:
    stmt = select(SessionInput).where(SessionInput.session_id == session_id)
    if not include_terminal:
        stmt = stmt.where(
            SessionInput.status.in_(("queued", "applying", "blocked"))
        )
    result = await db.execute(
        stmt.order_by(SessionInput.position.asc(), SessionInput.time_created.asc())
    )
    return list(result.scalars().all())


async def claim_next_session_input(
    db: AsyncSession,
    session_id: str,
    *,
    mode: InputMode,
    target_stream_id: str | None = None,
) -> SessionInput | None:
    """Atomically move the first eligible queued item to ``applying``."""
    stmt = select(SessionInput.id).where(
        SessionInput.session_id == session_id,
        SessionInput.mode == mode,
        SessionInput.status == "queued",
    )
    if mode == "steer" and target_stream_id is not None:
        stmt = stmt.where(SessionInput.target_stream_id == target_stream_id)
    candidate_id = (
        await db.execute(
            stmt.order_by(SessionInput.position.asc(), SessionInput.time_created.asc()).limit(1)
        )
    ).scalar_one_or_none()
    if candidate_id is None:
        return None

    claimed = await db.execute(
        update(SessionInput)
        .where(
            SessionInput.id == candidate_id,
            SessionInput.status == "queued",
        )
        .values(status="applying", time_applied=datetime.now(timezone.utc))
    )
    if claimed.rowcount != 1:
        return None
    await db.flush()
    return await db.get(SessionInput, candidate_id)


async def claim_next_generation_input(
    db: AsyncSession,
    session_id: str,
    *,
    target_stream_id: str,
) -> SessionInput | None:
    """Claim the next runnable follow-up for a generation stream.

    Normal queue entries are always runnable. A steer is normally consumed by
    ``SessionPrompt`` at a model/tool safe boundary; if it arrives after the
    final boundary, this fallback runs it as the next user turn on the same
    stream instead of leaving it stranded forever.
    """
    candidate_id = (
        await db.execute(
            select(SessionInput.id)
            .where(
                SessionInput.session_id == session_id,
                SessionInput.status == "queued",
                or_(
                    SessionInput.mode == "queue",
                    and_(
                        SessionInput.mode == "steer",
                        SessionInput.target_stream_id == target_stream_id,
                    ),
                ),
            )
            .order_by(SessionInput.position.asc(), SessionInput.time_created.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if candidate_id is None:
        return None

    claimed = await db.execute(
        update(SessionInput)
        .where(
            SessionInput.id == candidate_id,
            SessionInput.status == "queued",
        )
        .values(status="applying", time_applied=datetime.now(timezone.utc))
    )
    if claimed.rowcount != 1:
        return None
    await db.flush()
    return await db.get(SessionInput, candidate_id)


async def finish_session_input(
    db: AsyncSession,
    item_id: str,
    *,
    status: Literal["consumed", "cancelled", "failed", "blocked"],
    applied_stream_id: str | None = None,
    error_message: str | None = None,
) -> SessionInput | None:
    item = await db.get(SessionInput, item_id)
    if item is None:
        return None
    if item.status in {"consumed", "cancelled"}:
        return item
    item.status = status
    item.applied_stream_id = applied_stream_id or item.applied_stream_id
    item.error_message = error_message
    if item.time_applied is None:
        item.time_applied = datetime.now(timezone.utc)
    await db.flush()
    return item


async def cancel_session_input(
    db: AsyncSession,
    session_id: str,
    item_id: str,
) -> bool:
    result = await db.execute(
        update(SessionInput)
        .where(
            SessionInput.id == item_id,
            SessionInput.session_id == session_id,
            SessionInput.status.in_(("queued", "blocked")),
        )
        .values(status="cancelled", time_applied=datetime.now(timezone.utc))
    )
    return result.rowcount == 1


async def update_queued_session_input(
    db: AsyncSession,
    session_id: str,
    item_id: str,
    *,
    mode: InputMode | None = None,
    target_stream_id: str | None = None,
    move: Literal["up", "down"] | None = None,
    position: int | None = None,
) -> SessionInput | None:
    """Change delivery mode or ordering while an input is still unclaimed."""

    items = list(
        (
            await db.execute(
                select(SessionInput)
                .where(
                    SessionInput.session_id == session_id,
                    SessionInput.status == "queued",
                )
                .order_by(SessionInput.position.asc(), SessionInput.time_created.asc())
            )
        )
        .scalars()
        .all()
    )
    index = next((i for i, item in enumerate(items) if item.id == item_id), None)
    if index is None:
        return None

    item = items[index]
    if mode is not None:
        item.mode = mode
        item.target_stream_id = target_stream_id if mode == "steer" else None

    if move is not None:
        neighbor_index = index - 1 if move == "up" else index + 1
        if 0 <= neighbor_index < len(items):
            neighbor = items[neighbor_index]
            item.position, neighbor.position = neighbor.position, item.position

    if position is not None:
        items.pop(index)
        target_index = min(max(position - 1, 0), len(items))
        items.insert(target_index, item)
        for new_position, queued_item in enumerate(items, start=1):
            queued_item.position = new_position

    await db.flush()
    return item


async def block_interrupted_inputs(db: AsyncSession) -> int:
    """Prevent an interrupted, possibly side-effecting input from auto-replay."""
    result = await db.execute(
        update(SessionInput)
        .where(SessionInput.status == "applying")
        .values(
            status="blocked",
            error_message="Application exited before this input completed",
        )
    )
    return int(result.rowcount or 0)


async def block_unstarted_inputs_for_stream(
    db: AsyncSession,
    *,
    session_id: str,
    stream_id: str,
    error_message: str,
) -> int:
    """Make follow-ups terminal when their owning generation never starts."""

    result = await db.execute(
        update(SessionInput)
        .where(
            SessionInput.session_id == session_id,
            SessionInput.status == "queued",
            or_(
                SessionInput.mode == "queue",
                and_(
                    SessionInput.mode == "steer",
                    SessionInput.target_stream_id == stream_id,
                ),
            ),
        )
        .values(
            status="blocked",
            error_message=error_message,
        )
    )
    return int(result.rowcount or 0)
