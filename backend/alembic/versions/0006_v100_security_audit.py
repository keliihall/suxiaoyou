"""Add the v1.0 append-only security audit store.

Revision ID: 0006_v100_security_audit
Revises: 0005_v090_release_boundary
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006_v100_security_audit"
down_revision = "0005_v090_release_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "security_audit_event",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("time_created", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("time_updated", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_security_audit_time", "security_audit_event", ["time_created"])
    op.create_index(
        "ix_security_audit_source",
        "security_audit_event",
        ["source_kind", "source_id"],
    )
    op.create_index("ix_security_audit_session", "security_audit_event", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_security_audit_session", table_name="security_audit_event")
    op.drop_index("ix_security_audit_source", table_name="security_audit_event")
    op.drop_index("ix_security_audit_time", table_name="security_audit_event")
    op.drop_table("security_audit_event")
