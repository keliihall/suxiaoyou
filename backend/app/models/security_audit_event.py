"""Append-only security audit events for privileged capability use."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid


class SecurityAuditEvent(Base, TimestampMixin):
    """A redacted security decision or privileged action outcome.

    Arguments, prompts, credentials, file contents, and provider responses are
    deliberately excluded.  The event is useful for answering who/what/when
    without turning the audit store into a second secret-bearing transcript.
    """

    __tablename__ = "security_audit_event"
    __table_args__ = (
        Index("ix_security_audit_time", "time_created"),
        Index("ix_security_audit_source", "source_kind", "source_id"),
        Index(
            "ix_security_audit_invocation_source",
            "invocation_source_kind",
            "invocation_source_id",
        ),
        Index("ix_security_audit_session", "session_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    # Nullable only for rows written by v1.0 builds before the invocation
    # provenance migration.  New events always populate the kind.
    invocation_source_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    invocation_source_id: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
