"""Add workspace-scoped user Office template approval records.

Revision ID: 0011_v110_user_office_templates
Revises: 0010_v110_checkpoint_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0011_v110_user_office_templates"
down_revision = "0010_v110_checkpoint_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "office_user_template",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("template_ref", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("workspace_instance_id", sa.String(), nullable=False),
        sa.Column("created_by_session_id", sa.String(), nullable=True),
        sa.Column(
            "import_idempotency_key", sa.String(length=160), nullable=False
        ),
        sa.Column("import_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_size_bytes", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("placeholder_schema", sa.JSON(), nullable=False),
        sa.Column("placeholder_parts", sa.JSON(), nullable=False),
        sa.Column("allowed_operations", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("render_quality", sa.String(length=16), nullable=False),
        sa.Column("renderer_id", sa.String(length=256), nullable=False),
        sa.Column("renderer_version", sa.String(length=256), nullable=False),
        sa.Column("font_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "render_parameters_version", sa.String(length=256), nullable=False
        ),
        sa.Column(
            "render_parameters_sha256", sa.String(length=64), nullable=False
        ),
        sa.Column("render_cache_key", sa.String(length=64), nullable=False),
        sa.Column(
            "render_manifest_sha256", sa.String(length=64), nullable=False
        ),
        sa.Column("render_page_count", sa.Integer(), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("time_approved", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_tombstoned", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "time_created",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "time_updated",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_office_user_template_revision"
        ),
        sa.CheckConstraint(
            "state_version >= 1", name="ck_office_user_template_state_version"
        ),
        sa.CheckConstraint(
            "format IN ('docx', 'xlsx', 'pptx')",
            name="ck_office_user_template_format",
        ),
        sa.CheckConstraint(
            "status IN ('needs_confirmation', 'needs_review', 'approved', "
            "'tombstoned')",
            name="ck_office_user_template_status",
        ),
        sa.CheckConstraint(
            "render_quality IN ('authoritative', 'approximate')",
            name="ck_office_user_template_render_quality",
        ),
        sa.CheckConstraint(
            "source_size_bytes >= 1",
            name="ck_office_user_template_source_size",
        ),
        sa.CheckConstraint(
            "render_page_count >= 1",
            name="ck_office_user_template_render_page_count",
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND render_quality = 'authoritative' "
            "AND time_approved IS NOT NULL) OR "
            "(status IN ('needs_confirmation', 'needs_review') "
            "AND time_approved IS NULL) OR status = 'tombstoned'",
            name="ck_office_user_template_approval_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'tombstoned' AND time_tombstoned IS NOT NULL) OR "
            "(status != 'tombstoned' AND time_tombstoned IS NULL)",
            name="ck_office_user_template_tombstone_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_session_id"], ["session.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_instance_id"],
            ["workspace_instance.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_ref",
            "revision",
            name="uq_office_user_template_ref_revision",
        ),
        sa.UniqueConstraint(
            "workspace_instance_id",
            "import_idempotency_key",
            name="uq_office_user_template_workspace_idempotency",
        ),
    )
    op.create_index(
        "ix_office_user_template_workspace_status",
        "office_user_template",
        ["workspace_instance_id", "status", "time_created"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_office_user_template_workspace_status",
        table_name="office_user_template",
    )
    op.drop_table("office_user_template")
