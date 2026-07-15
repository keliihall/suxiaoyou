from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.security_audit_event import SecurityAuditEvent
from app.security import audit as audit_module
from app.security.audit import AuditPersistenceError, record_security_event


@pytest.mark.asyncio
async def test_audit_event_is_persistent_bounded_and_redacted(session_factory) -> None:
    await record_security_event(
        session_factory,
        source_kind="connector",
        source_id="tencent-docs",
        invocation_source_kind="scheduler",
        invocation_source_id="task-1",
        capability="remote_data",
        action="update",
        decision="ask",
        outcome="success",
        session_id="session",
        call_id="call",
        details={
            "tool_id": "append_text",
            "token": "must-not-persist",
            "api_key_hint": "must-not-persist",
            "count": 2,
        },
    )

    async with session_factory() as db:
        event = (await db.execute(select(SecurityAuditEvent))).scalar_one()
    assert event.source_id == "tencent-docs"
    assert event.invocation_source_kind == "scheduler"
    assert event.invocation_source_id == "task-1"
    assert event.details == {"tool_id": "append_text", "count": 2}
    assert "must-not-persist" not in str(event.details)


@pytest.mark.asyncio
async def test_invalid_taxonomy_values_fail_closed(session_factory) -> None:
    await record_security_event(
        session_factory,
        source_kind="x",
        source_id="x",
        capability="x",
        action="x",
        decision="unexpected",
        outcome="unexpected",
    )
    async with session_factory() as db:
        event = (await db.execute(select(SecurityAuditEvent))).scalar_one()
    assert event.decision == "system"
    assert event.outcome == "error"


@pytest.mark.asyncio
async def test_audit_retention_keeps_only_the_newest_bounded_events(
    session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(audit_module, "MAX_AUDIT_EVENTS", 2)
    ids = iter(("event-1", "event-2", "event-3"))
    monkeypatch.setattr(audit_module, "generate_ulid", lambda: next(ids))
    for index in range(3):
        await record_security_event(
            session_factory,
            source_kind="builtin",
            source_id="suyo",
            capability="network",
            action=f"action-{index}",
            decision="allow",
            outcome="success",
        )

    async with session_factory() as db:
        events = (
            await db.execute(
                select(SecurityAuditEvent).order_by(SecurityAuditEvent.id)
            )
        ).scalars().all()
    assert len(events) == 2
    assert {event.action for event in events} == {"action-1", "action-2"}


@pytest.mark.asyncio
async def test_required_audit_failure_raises_but_diagnostic_event_stays_best_effort() -> None:
    def broken_session_factory():
        raise RuntimeError("database unavailable")

    # A diagnostic/outcome write cannot retroactively undo an action.
    await record_security_event(
        broken_session_factory,  # type: ignore[arg-type]
        source_kind="builtin",
        source_id="suyo",
        capability="filesystem_read",
        action="read",
        decision="allow",
        outcome="error",
    )

    with pytest.raises(AuditPersistenceError, match="could not be persisted"):
        await record_security_event(
            broken_session_factory,  # type: ignore[arg-type]
            source_kind="builtin",
            source_id="suyo",
            capability="filesystem_write",
            action="write",
            decision="allow",
            outcome="started",
            required=True,
        )
