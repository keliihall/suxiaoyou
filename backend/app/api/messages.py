"""Message listing endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_db
from app.models.message import Message, Part
from app.schemas.message import (
    ConversationTurn,
    ConversationTurnIndex,
    MessageResponse,
    PaginatedMessages,
    PartResponse,
)
from app.session.manager import count_messages, get_messages

router = APIRouter()

_TURN_SUMMARY_MAX_CHARS = 160


def _msg_to_response(msg: Message) -> MessageResponse:
    return MessageResponse(
        id=msg.id,
        session_id=msg.session_id,
        time_created=msg.time_created,
        data=msg.data or {},
        parts=[
            PartResponse(
                id=p.id,
                message_id=p.message_id,
                session_id=p.session_id,
                time_created=p.time_created,
                data=p.data or {},
            )
            for p in msg.parts
        ],
    )


@router.get("/messages/{session_id}", response_model=PaginatedMessages)
async def list_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=-1),
    db: AsyncSession = Depends(get_db),
) -> PaginatedMessages:
    """Get messages for a session with pagination.

    offset=-1 (default) returns the latest page.
    """
    total = await count_messages(db, session_id)
    actual_offset = max(0, total - limit) if offset < 0 else offset
    messages = await get_messages(db, session_id, limit=limit, offset=actual_offset)
    return PaginatedMessages(
        total=total,
        offset=actual_offset,
        messages=[_msg_to_response(msg) for msg in messages],
    )


def _turn_summary(text_parts: list[str], attachment_names: list[str]) -> str:
    """Build a stable, single-line local summary without invoking a model."""
    normalized = " ".join(" ".join(text_parts).split())
    if not normalized:
        normalized = attachment_names[0] if attachment_names else ""
    if len(normalized) <= _TURN_SUMMARY_MAX_CHARS:
        return normalized
    return normalized[: _TURN_SUMMARY_MAX_CHARS - 1].rstrip() + "…"


@router.get(
    "/messages/{session_id}/turn-index",
    response_model=ConversationTurnIndex,
)
async def get_turn_index(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> ConversationTurnIndex:
    """Return navigation metadata for every visible user turn in a session.

    Only message metadata plus user text/file parts are read. Assistant parts,
    reasoning, and tool output never enter the response. ``message_offset`` is
    the message's stable chronological offset and lets the client request the
    containing message page directly.
    """
    message_rows = (
        await db.execute(
            select(Message.id, Message.time_created, Message.data)
            .where(Message.session_id == session_id)
            .order_by(Message.time_created.asc(), Message.id.asc())
        )
    ).all()

    visible_users: list[tuple[str, datetime, int]] = []
    for message_offset, row in enumerate(message_rows):
        data = row.data or {}
        if data.get("role") == "user" and not data.get("system"):
            visible_users.append((row.id, row.time_created, message_offset))

    if not visible_users:
        return ConversationTurnIndex(
            total_messages=len(message_rows),
            total_turns=0,
            turns=[],
        )

    user_ids = [message_id for message_id, _, _ in visible_users]
    texts_by_message: dict[str, list[str]] = {message_id: [] for message_id in user_ids}
    files_by_message: dict[str, list[str]] = {message_id: [] for message_id in user_ids}
    # SQLite commonly limits bound parameters to 999. Batch user IDs so very
    # long conversations remain indexable without ever selecting assistant
    # reasoning/tool parts as a fallback.
    for batch_start in range(0, len(user_ids), 500):
        batch = user_ids[batch_start : batch_start + 500]
        part_rows = (
            await db.execute(
                select(Part.message_id, Part.data)
                .where(Part.message_id.in_(batch))
                .order_by(Part.time_created.asc(), Part.id.asc())
            )
        ).all()
        for row in part_rows:
            data = row.data or {}
            if data.get("type") == "text":
                text = data.get("text")
                if isinstance(text, str) and text:
                    texts_by_message[row.message_id].append(text)
            elif data.get("type") == "file":
                name = data.get("name")
                if isinstance(name, str) and name:
                    files_by_message[row.message_id].append(name)

    turns = [
        ConversationTurn(
            message_id=message_id,
            ordinal=ordinal,
            message_offset=message_offset,
            time_created=time_created,
            summary=_turn_summary(
                texts_by_message[message_id],
                files_by_message[message_id],
            ),
            attachment_names=files_by_message[message_id],
        )
        for ordinal, (message_id, time_created, message_offset) in enumerate(
            visible_users,
            start=1,
        )
    ]
    return ConversationTurnIndex(
        total_messages=len(message_rows),
        total_turns=len(turns),
        turns=turns,
    )


@router.get("/messages/{session_id}/{message_id}", response_model=MessageResponse)
async def get_message(
    session_id: str,
    message_id: str,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Get a single message with its parts."""
    stmt = (
        select(Message)
        .where(Message.id == message_id)
        .options(selectinload(Message.parts))
    )
    msg = (await db.execute(stmt)).scalar_one_or_none()
    if msg is None or msg.session_id != session_id:
        raise HTTPException(status_code=404, detail="Message not found")

    return _msg_to_response(msg)
