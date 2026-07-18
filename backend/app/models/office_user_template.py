"""Workspace-scoped approval records for user-imported Office templates."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
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


class OfficeUserTemplate(Base, TimestampMixin):
    """Immutable source revision plus mutable, CAS-protected approval state.

    The source package itself lives in the private content-addressed template
    registry.  This row intentionally stores no host path.  ``revision`` binds
    immutable source/schema/render evidence, while ``state_version`` changes
    only for approval or tombstone transitions.
    """

    __tablename__ = "office_user_template"
    __table_args__ = (
        UniqueConstraint(
            "template_ref",
            "revision",
            name="uq_office_user_template_ref_revision",
        ),
        UniqueConstraint(
            "workspace_instance_id",
            "import_idempotency_key",
            name="uq_office_user_template_workspace_idempotency",
        ),
        Index(
            "ix_office_user_template_workspace_status",
            "workspace_instance_id",
            "status",
            "time_created",
        ),
        CheckConstraint("revision >= 1", name="ck_office_user_template_revision"),
        CheckConstraint(
            "state_version >= 1",
            name="ck_office_user_template_state_version",
        ),
        CheckConstraint(
            "format IN ('docx', 'xlsx', 'pptx')",
            name="ck_office_user_template_format",
        ),
        CheckConstraint(
            "status IN ('needs_confirmation', 'needs_review', 'approved', "
            "'tombstoned')",
            name="ck_office_user_template_status",
        ),
        CheckConstraint(
            "render_quality IN ('authoritative', 'approximate')",
            name="ck_office_user_template_render_quality",
        ),
        CheckConstraint(
            "source_size_bytes >= 1",
            name="ck_office_user_template_source_size",
        ),
        CheckConstraint(
            "render_page_count >= 1",
            name="ck_office_user_template_render_page_count",
        ),
        CheckConstraint(
            "(status = 'approved' AND render_quality = 'authoritative' "
            "AND time_approved IS NOT NULL) OR "
            "(status IN ('needs_confirmation', 'needs_review') "
            "AND time_approved IS NULL) OR status = 'tombstoned'",
            name="ck_office_user_template_approval_lifecycle",
        ),
        CheckConstraint(
            "(status = 'tombstoned' AND time_tombstoned IS NOT NULL) OR "
            "(status != 'tombstoned' AND time_tombstoned IS NULL)",
            name="ck_office_user_template_tombstone_lifecycle",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_ulid)
    template_ref: Mapped[str] = mapped_column(String(length=64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    workspace_instance_id: Mapped[str] = mapped_column(
        ForeignKey("workspace_instance.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("session.id", ondelete="SET NULL"), nullable=True
    )
    import_idempotency_key: Mapped[str] = mapped_column(
        String(length=160), nullable=False
    )
    import_request_sha256: Mapped[str] = mapped_column(
        String(length=64), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(length=160), nullable=False)
    format: Mapped[str] = mapped_column(String(length=8), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(length=64), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(length=64), nullable=False)
    placeholder_schema: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    placeholder_parts: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    allowed_operations: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: ["instantiate_text"]
    )
    status: Mapped[str] = mapped_column(String(length=32), nullable=False)
    render_quality: Mapped[str] = mapped_column(String(length=16), nullable=False)
    renderer_id: Mapped[str] = mapped_column(String(length=256), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(length=256), nullable=False)
    font_digest: Mapped[str] = mapped_column(String(length=64), nullable=False)
    render_parameters_version: Mapped[str] = mapped_column(
        String(length=256), nullable=False
    )
    render_parameters_sha256: Mapped[str] = mapped_column(
        String(length=64), nullable=False
    )
    render_cache_key: Mapped[str] = mapped_column(String(length=64), nullable=False)
    render_manifest_sha256: Mapped[str] = mapped_column(
        String(length=64), nullable=False
    )
    render_page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_report: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    time_approved: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    time_tombstoned: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["OfficeUserTemplate"]
