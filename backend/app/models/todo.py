"""Todo model — session-scoped task list for tracking agent progress."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid

if TYPE_CHECKING:
    from app.models.session import Session
    from app.models.session_goal import SessionGoal


class Todo(Base, TimestampMixin):
    __tablename__ = "todo"
    __table_args__ = (
        ForeignKeyConstraint(
            ["goal_id", "session_id"],
            ["session_goal.id", "session_goal.session_id"],
            name="fk_todo_goal_session",
            ondelete="CASCADE",
        ),
        Index("ix_todo_goal", "goal_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    goal_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    content: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    active_form: Mapped[str] = mapped_column(String, nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    session: Mapped[Session] = relationship(overlaps="goal,todos")
    goal: Mapped[SessionGoal | None] = relationship(
        back_populates="todos",
        overlaps="session",
    )
