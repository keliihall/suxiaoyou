"""Ordered filesystem mutations retained by a session checkpoint."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.id import generate_ulid


class CheckpointChange(Base, TimestampMixin):
    """One reversible path transition, replayed in reverse sequence order."""

    __tablename__ = "checkpoint_change"
    __table_args__ = (
        UniqueConstraint(
            "checkpoint_id", "sequence", name="uq_checkpoint_change_sequence"
        ),
        Index("ix_checkpoint_change_checkpoint_path", "checkpoint_id", "relative_path"),
        Index("ix_checkpoint_change_turn", "turn_run_id"),
        CheckConstraint("sequence >= 1", name="ck_checkpoint_change_sequence"),
        CheckConstraint(
            "operation IN ('created', 'modified', 'deleted')",
            name="ck_checkpoint_change_operation",
        ),
        CheckConstraint(
            "node_kind IN ('file', 'directory', 'symlink')",
            name="ck_checkpoint_change_node_kind",
        ),
        CheckConstraint(
            "node_kind != 'symlink' OR operation = 'created'",
            name="ck_checkpoint_change_symlink_created_only",
        ),
        CheckConstraint(
            "(operation = 'created' AND before_exists = false AND after_exists = true) OR "
            "(operation = 'modified' AND before_exists = true AND after_exists = true) OR "
            "(operation = 'deleted' AND before_exists = true AND after_exists = false)",
            name="ck_checkpoint_change_existence",
        ),
        CheckConstraint(
            "before_version_id IS NULL OR "
            "(before_exists = true AND node_kind = 'file')",
            name="ck_checkpoint_change_before_version_kind",
        ),
        CheckConstraint(
            "node_kind = 'directory' OR operation = 'created' OR "
            "before_version_id IS NOT NULL",
            name="ck_checkpoint_change_file_restore_source",
        ),
        CheckConstraint(
            "(node_kind = 'directory' AND before_sha256 IS NULL AND after_sha256 IS NULL) OR "
            "(node_kind = 'file' AND operation = 'created' AND "
            "before_sha256 IS NULL AND after_sha256 IS NOT NULL) OR "
            "(node_kind = 'file' AND operation = 'modified' AND "
            "before_sha256 IS NOT NULL AND after_sha256 IS NOT NULL) OR "
            "(node_kind = 'file' AND operation = 'deleted' AND "
            "before_sha256 IS NOT NULL AND after_sha256 IS NULL) OR "
            "(node_kind = 'symlink' AND operation = 'created' AND "
            "before_sha256 IS NULL AND after_sha256 IS NOT NULL)",
            name="ck_checkpoint_change_hashes",
        ),
        CheckConstraint(
            "(before_exists = false AND before_mode IS NULL) OR before_exists = true",
            name="ck_checkpoint_change_before_mode",
        ),
        CheckConstraint(
            "(after_exists = false AND after_mode IS NULL AND after_size IS NULL) OR "
            "after_exists = true",
            name="ck_checkpoint_change_after_metadata",
        ),
        CheckConstraint(
            "before_mode IS NULL OR before_mode >= 0",
            name="ck_checkpoint_change_before_mode_nonnegative",
        ),
        CheckConstraint(
            "after_mode IS NULL OR after_mode >= 0",
            name="ck_checkpoint_change_after_mode_nonnegative",
        ),
        CheckConstraint(
            "after_size IS NULL OR after_size >= 0",
            name="ck_checkpoint_change_after_size_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    checkpoint_id: Mapped[str] = mapped_column(
        ForeignKey("session_checkpoint.id", ondelete="CASCADE"), nullable=False
    )
    turn_run_id: Mapped[str] = mapped_column(
        ForeignKey("turn_run.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(length=16), nullable=False)
    node_kind: Mapped[str] = mapped_column(
        String(length=16), nullable=False, default="file", server_default="file"
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    before_exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    before_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    before_sha256: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    before_mode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    after_sha256: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    after_mode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
