"""Session checkpoint grouped around one root turn."""

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


class SessionCheckpoint(Base, TimestampMixin):
    """A turn-owned rewind boundary, aggregated under ``root_turn_id``."""

    __tablename__ = "session_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "sequence", name="uq_session_checkpoint_sequence"
        ),
        UniqueConstraint("turn_run_id", name="uq_session_checkpoint_turn_run"),
        Index("ix_session_checkpoint_root_turn", "root_turn_id", "sequence"),
        Index(
            "ix_session_checkpoint_session_state",
            "session_id",
            "state",
            "sequence",
        ),
        Index(
            "ix_session_checkpoint_workspace_pin",
            "workspace_instance_id",
            "pin_state",
        ),
        CheckConstraint("sequence >= 1", name="ck_session_checkpoint_sequence"),
        CheckConstraint(
            "state IN ('prepared', 'committing', 'finalized', 'rewinding', "
            "'rewound', 'failed')",
            name="ck_session_checkpoint_state",
        ),
        CheckConstraint(
            "pin_state IN ('pinned', 'released')",
            name="ck_session_checkpoint_pin_state",
        ),
        CheckConstraint(
            "(pin_state = 'pinned' AND time_pin_released IS NULL) OR "
            "(pin_state = 'released' AND time_pin_released IS NOT NULL)",
            name="ck_session_checkpoint_pin_lifecycle",
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
    turn_run_id: Mapped[str] = mapped_column(
        ForeignKey("turn_run.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    goal_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    todo_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    child_turn_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    state: Mapped[str] = mapped_column(
        String(length=24),
        nullable=False,
        default="prepared",
        server_default="prepared",
    )
    pin_state: Mapped[str] = mapped_column(
        String(length=24), nullable=False, default="pinned", server_default="pinned"
    )
    has_irreversible_side_effects: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    external_side_effects: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    time_finalized: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    time_rewound: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    time_pin_released: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
