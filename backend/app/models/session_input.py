"""Persistent user inputs submitted while a conversation is busy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid


class SessionInput(Base, TimestampMixin):
    __tablename__ = "session_input"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_session_input_client_request",
        ),
        Index("ix_session_input_dispatch", "session_id", "status", "position"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="queue")
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    # Snapshot the execution choices at enqueue time so later global settings
    # changes cannot silently alter a queued request.
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent: Mapped[str] = mapped_column(String, nullable=False, default="build")
    language: Mapped[str] = mapped_column(String, nullable=False, default="zh")
    workspace: Mapped[str | None] = mapped_column(String, nullable=True)
    reasoning: Mapped[bool | None] = mapped_column(nullable=True)
    permission_presets: Mapped[dict[str, bool] | None] = mapped_column(JSON, nullable=True)
    permission_rules: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    target_stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_applied: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
