"""Root-turn and delegated child execution ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid


class TurnRun(Base, TimestampMixin):
    """One root user turn or a child Agent run attributed to that turn."""

    __tablename__ = "turn_run"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_turn_run_session_idempotency",
        ),
        Index("ix_turn_run_root_status", "root_turn_id", "status"),
        Index("ix_turn_run_session_created", "session_id", "time_created"),
        CheckConstraint("depth >= 0", name="ck_turn_run_depth"),
        CheckConstraint(
            "(parent_turn_id IS NULL AND root_turn_id = id AND depth = 0) OR "
            "(parent_turn_id IS NOT NULL AND root_turn_id != id AND depth > 0)",
            name="ck_turn_run_root_parent",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled', 'rewound')",
            name="ck_turn_run_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    workspace_instance_id: Mapped[str] = mapped_column(
        ForeignKey("workspace_instance.id", ondelete="RESTRICT"), nullable=False
    )
    root_turn_id: Mapped[str] = mapped_column(
        ForeignKey("turn_run.id", ondelete="CASCADE"), nullable=False
    )
    parent_turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("turn_run.id", ondelete="CASCADE"), nullable=True
    )
    depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source_kind: Mapped[str] = mapped_column(
        String(length=40), nullable=False, default="interactive", server_default="interactive"
    )
    status: Mapped[str] = mapped_column(
        String(length=24), nullable=False, default="running", server_default="running"
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(length=160), nullable=True
    )
    request_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    response_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    has_irreversible_side_effects: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    external_side_effects: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    time_started: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    time_finished: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
