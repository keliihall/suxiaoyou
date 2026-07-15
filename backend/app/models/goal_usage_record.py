"""Idempotent per-source usage records for an in-flight GoalRun."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid

if TYPE_CHECKING:
    from app.models.goal_run import GoalRun


class GoalUsageRecord(Base, TimestampMixin):
    __tablename__ = "goal_usage_record"
    __table_args__ = (
        Index("ix_goal_usage_run", "goal_run_id"),
        UniqueConstraint(
            "goal_run_id",
            "source_key",
            name="uq_goal_usage_run_source",
        ),
        CheckConstraint("tokens_used >= 0", name="ck_goal_usage_tokens"),
        CheckConstraint("cost_used_microusd >= 0", name="ck_goal_usage_cost"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    goal_run_id: Mapped[str] = mapped_column(
        ForeignKey("goal_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(String(length=32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(length=255), nullable=False)
    tokens_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    cost_used_microusd: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    run: Mapped[GoalRun] = relationship(back_populates="usage_records")
