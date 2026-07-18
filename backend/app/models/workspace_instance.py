"""Durable identity for one concrete workspace filesystem instance."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid


class WorkspaceInstance(Base, TimestampMixin):
    """A path plus filesystem identity, not merely a user-selected pathname.

    A directory may be deleted and recreated at the same path.  Keeping the
    identity token separate prevents a replacement directory from inheriting
    checkpoints that belong to the previous filesystem object.  ``kind`` is
    deliberately open-ended so the later worktree/managed-copy providers can
    add values without another schema migration.
    """

    __tablename__ = "workspace_instance"
    __table_args__ = (
        UniqueConstraint(
            "root_path",
            "identity_token",
            name="uq_workspace_instance_root_identity",
        ),
        Index("ix_workspace_instance_project_status", "project_id", "status"),
        CheckConstraint(
            "status IN ('active', 'released', 'missing')",
            name="ck_workspace_instance_status",
        ),
        CheckConstraint(
            "(status = 'active' AND time_released IS NULL) OR "
            "(status != 'active' AND time_released IS NOT NULL)",
            name="ck_workspace_instance_release_lifecycle",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("project.id", ondelete="SET NULL"), nullable=True
    )
    parent_instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace_instance.id", ondelete="SET NULL"), nullable=True
    )
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("session.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(
        String(length=40), nullable=False, default="direct", server_default="direct"
    )
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    identity_token: Mapped[str] = mapped_column(String(length=255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(length=24), nullable=False, default="active", server_default="active"
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    time_released: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
