"""Durable responses for idempotent state-changing API operations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid


class IdempotencyRecord(Base, TimestampMixin):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint("scope", "request_key", name="uq_idempotency_scope_key"),
        Index("ix_idempotency_status", "scope", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    request_key: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="accepted")
    response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
