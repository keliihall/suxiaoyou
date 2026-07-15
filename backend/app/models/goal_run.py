"""Durable execution ledger for individual Goal work slices."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid

if TYPE_CHECKING:
    from app.models.goal_usage_record import GoalUsageRecord
    from app.models.session_goal import SessionGoal


_ACTIVE_RUN_SQL = "status IN ('reserved', 'running', 'waiting_user')"


class GoalRun(Base, TimestampMixin):
    """One initial, resumed, user-input, or autonomous Goal execution slice."""

    __tablename__ = "goal_run"
    __table_args__ = (
        UniqueConstraint("goal_id", "ordinal", name="uq_goal_run_ordinal"),
        UniqueConstraint("idempotency_key", name="uq_goal_run_idempotency_key"),
        Index("ix_goal_run_goal_status", "goal_id", "status"),
        Index(
            "uq_goal_run_one_active",
            "goal_id",
            unique=True,
            sqlite_where=text(_ACTIVE_RUN_SQL),
            postgresql_where=text(_ACTIVE_RUN_SQL),
        ),
        CheckConstraint(
            "trigger IN ('initial', 'auto', 'resume', 'user_input')",
            name="ck_goal_run_trigger",
        ),
        CheckConstraint(
            "status IN ('reserved', 'running', 'waiting_user', 'completed', "
            "'blocked', 'interrupted', 'failed')",
            name="ck_goal_run_status",
        ),
        CheckConstraint("ordinal >= 1", name="ck_goal_run_ordinal"),
        CheckConstraint("goal_revision >= 1", name="ck_goal_run_revision"),
        CheckConstraint("tokens_used >= 0", name="ck_goal_run_tokens_used"),
        CheckConstraint(
            "cost_used_microusd >= 0", name="ck_goal_run_cost_used"
        ),
        CheckConstraint("active_seconds >= 0", name="ck_goal_run_active_seconds"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("session_goal.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(length=128), nullable=False)
    stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger: Mapped[str] = mapped_column(String(length=32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(length=32), nullable=False, default="reserved", server_default="reserved"
    )

    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_used_microusd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    active_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    progress_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(length=160), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(length=80), nullable=True)

    lease_owner: Mapped[str | None] = mapped_column(String(length=160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    side_effects_started: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    time_started: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    time_finished: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    goal: Mapped[SessionGoal] = relationship(back_populates="runs")
    usage_records: Mapped[list[GoalUsageRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
