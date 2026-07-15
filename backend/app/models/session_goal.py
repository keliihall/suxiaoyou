"""Persistent, session-scoped completion contract for Goal mode."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid

if TYPE_CHECKING:
    from app.models.goal_run import GoalRun
    from app.models.session import Session
    from app.models.todo import Todo


class SessionGoal(Base, TimestampMixin):
    """The single durable Goal that may be attached to a conversation."""

    __tablename__ = "session_goal"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session_goal_session"),
        UniqueConstraint("id", "session_id", name="uq_session_goal_id_session"),
        CheckConstraint(
            "status IN ('active', 'paused', 'blocked', 'usage_limited', "
            "'budget_limited', 'complete')",
            name="ck_session_goal_status",
        ),
        CheckConstraint(
            "run_state IN ('idle', 'reserved', 'running', 'pausing', "
            "'waiting_user', 'interrupted')",
            name="ck_session_goal_run_state",
        ),
        CheckConstraint("revision >= 1", name="ck_session_goal_revision"),
        CheckConstraint(
            "length(objective) + length(coalesce(definition_of_done, '')) <= 4000",
            name="ck_session_goal_content_length",
        ),
        CheckConstraint(
            "token_budget IS NULL OR token_budget >= 0",
            name="ck_session_goal_token_budget",
        ),
        CheckConstraint("tokens_used >= 0", name="ck_session_goal_tokens_used"),
        CheckConstraint(
            "cost_budget_microusd IS NULL OR cost_budget_microusd >= 0",
            name="ck_session_goal_cost_budget",
        ),
        CheckConstraint(
            "cost_used_microusd >= 0", name="ck_session_goal_cost_used"
        ),
        CheckConstraint(
            "time_budget_seconds IS NULL OR time_budget_seconds >= 0",
            name="ck_session_goal_time_budget",
        ),
        CheckConstraint(
            "time_used_seconds >= 0", name="ck_session_goal_time_used"
        ),
        CheckConstraint(
            "max_continuations IS NULL OR max_continuations >= 0",
            name="ck_session_goal_max_continuations",
        ),
        CheckConstraint(
            "continuation_count >= 0", name="ck_session_goal_continuation_count"
        ),
        CheckConstraint(
            "no_progress_count >= 0", name="ck_session_goal_no_progress_count"
        ),
        CheckConstraint(
            "blocker_streak >= 0", name="ck_session_goal_blocker_streak"
        ),
        CheckConstraint(
            "consecutive_error_count >= 0",
            name="ck_session_goal_consecutive_error_count",
        ),
        CheckConstraint(
            "status != 'complete' OR (completion_summary IS NOT NULL "
            "AND completion_evidence IS NOT NULL AND time_completed IS NOT NULL)",
            name="ck_session_goal_completion_contract",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    definition_of_done: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(length=32), nullable=False, default="active", server_default="active"
    )
    run_state: Mapped[str] = mapped_column(
        String(length=32), nullable=False, default="idle", server_default="idle"
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_budget_microusd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_used_microusd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    time_budget_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_used_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_continuations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    continuation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    no_progress_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    blocker_streak: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    consecutive_error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    blocker_code: Mapped[str | None] = mapped_column(String(length=80), nullable=True)
    blocker_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completion_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_evidence: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(
        JSON, nullable=True
    )

    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent: Mapped[str] = mapped_column(
        String(length=80), nullable=False, default="build", server_default="build"
    )
    reasoning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    language: Mapped[str] = mapped_column(
        String(length=8), nullable=False, default="zh", server_default="zh"
    )
    # Server-owned ceiling. It is deliberately omitted from public schemas.
    permission_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )

    last_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    time_started: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    time_completed: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    session: Mapped[Session] = relationship(back_populates="goal")
    runs: Mapped[list[GoalRun]] = relationship(
        back_populates="goal", cascade="all, delete-orphan"
    )
    todos: Mapped[list[Todo]] = relationship(
        back_populates="goal",
        cascade="all, delete-orphan",
        overlaps="session",
    )
