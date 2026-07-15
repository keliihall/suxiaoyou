"""Append-only persistence for redacted security events."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.security_audit_event import SecurityAuditEvent
from app.utils.id import generate_ulid

logger = logging.getLogger(__name__)

MAX_AUDIT_EVENTS = 10_000
AUDIT_RETENTION_DAYS = 90

_ALLOWED_DECISIONS = frozenset({"allow", "ask", "deny", "system"})
_ALLOWED_OUTCOMES = frozenset(
    {"started", "success", "error", "denied", "blocked", "cancelled", "timeout"}
)


class AuditPersistenceError(RuntimeError):
    """Raised when a required pre-action audit record cannot be committed."""


async def record_security_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_kind: str,
    source_id: str,
    invocation_source_kind: str = "unknown",
    invocation_source_id: str | None = None,
    capability: str,
    action: str,
    decision: str,
    outcome: str,
    session_id: str | None = None,
    call_id: str | None = None,
    details: dict[str, Any] | None = None,
    required: bool = False,
) -> None:
    """Persist a bounded, secret-free event.

    Ordinary outcome/diagnostic events remain best effort.  Callers must set
    ``required=True`` for the pre-action record of a privileged operation; an
    unavailable audit store then raises and the caller can fail closed before
    any external or mutating side effect begins.
    """

    if decision not in _ALLOWED_DECISIONS:
        decision = "system"
    if outcome not in _ALLOWED_OUTCOMES:
        outcome = "error"
    safe_details = _bounded_details(details or {})
    try:
        async with session_factory() as db:
            async with db.begin():
                db.add(
                    SecurityAuditEvent(
                        id=generate_ulid(),
                        source_kind=_bounded_label(source_kind, 32, "unknown"),
                        source_id=_bounded_label(source_id, 160, "unknown"),
                        invocation_source_kind=_bounded_label(
                            invocation_source_kind, 32, "unknown"
                        ),
                        invocation_source_id=_bounded_optional(
                            invocation_source_id, 160
                        ),
                        capability=_bounded_label(capability, 80, "unknown"),
                        action=_bounded_label(action, 80, "unknown"),
                        decision=decision,
                        outcome=outcome,
                        session_id=_bounded_optional(session_id, 128),
                        call_id=_bounded_optional(call_id, 128),
                        details=safe_details,
                    )
                )
                await db.flush()
                cutoff = datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS)
                await db.execute(
                    delete(SecurityAuditEvent).where(
                        SecurityAuditEvent.time_created < cutoff
                    )
                )
                overflow_ids = (
                    select(SecurityAuditEvent.id)
                    .order_by(
                        SecurityAuditEvent.time_created.desc(),
                        SecurityAuditEvent.id.desc(),
                    )
                    .offset(MAX_AUDIT_EVENTS)
                )
                await db.execute(
                    delete(SecurityAuditEvent).where(
                        SecurityAuditEvent.id.in_(overflow_ids)
                    )
                )
    except Exception as exc:
        logger.warning("Could not persist security audit event", exc_info=True)
        if required:
            raise AuditPersistenceError(
                "Required security audit event could not be persisted"
            ) from exc


def _bounded_label(value: str, limit: int, fallback: str) -> str:
    normalized = " ".join(str(value).split())
    return normalized[:limit] or fallback


def _bounded_optional(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())[:limit]
    return normalized or None


def _bounded_details(value: dict[str, Any]) -> dict[str, Any]:
    """Allow scalar operational metadata only; reject secret-bearing keys."""

    result: dict[str, Any] = {}
    for key, item in list(value.items())[:20]:
        normalized_key = str(key).strip()[:64]
        if not normalized_key or any(
            marker in normalized_key.lower()
            for marker in ("token", "secret", "password", "key", "authorization", "prompt", "content")
        ):
            continue
        if isinstance(item, bool) or item is None:
            result[normalized_key] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            result[normalized_key] = item
        elif isinstance(item, str):
            result[normalized_key] = " ".join(item.split())[:240]
    return result
