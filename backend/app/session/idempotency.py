"""Persistent idempotency helpers for side-effecting requests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency_record import IdempotencyRecord


class IdempotencyConflictError(ValueError):
    """A request key was reused for a different operation payload."""


def canonical_request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def get_idempotency_record(
    db: AsyncSession,
    *,
    scope: str,
    request_key: str,
) -> IdempotencyRecord | None:
    return (
        await db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.request_key == request_key,
            )
        )
    ).scalar_one_or_none()


def validate_idempotent_replay(
    record: IdempotencyRecord,
    *,
    request_hash: str,
) -> dict[str, Any]:
    if record.request_hash != request_hash:
        raise IdempotencyConflictError(
            "The request key was already used with a different payload"
        )
    return dict(record.response or {})


async def mark_idempotency_status(
    db: AsyncSession,
    record_id: str,
    *,
    status: str,
    error_message: str | None = None,
) -> None:
    await db.execute(
        update(IdempotencyRecord)
        .where(IdempotencyRecord.id == record_id)
        .values(status=status, error_message=error_message)
    )


async def interrupt_inflight_idempotency_records(db: AsyncSession) -> int:
    result = await db.execute(
        update(IdempotencyRecord)
        .where(IdempotencyRecord.status.in_(("accepted", "running")))
        .values(
            status="interrupted",
            error_message="Application exited before the request completed",
        )
    )
    return int(result.rowcount or 0)
