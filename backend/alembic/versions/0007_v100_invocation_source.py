"""Persist trusted invocation provenance on security audit events.

Revision ID: 0007_v100_invocation_source
Revises: 0006_v100_security_audit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_v100_invocation_source"
down_revision = "0006_v100_security_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("security_audit_event") as batch_op:
        batch_op.add_column(
            sa.Column("invocation_source_kind", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("invocation_source_id", sa.String(length=160), nullable=True)
        )
        batch_op.create_index(
            "ix_security_audit_invocation_source",
            ["invocation_source_kind", "invocation_source_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("security_audit_event") as batch_op:
        batch_op.drop_index("ix_security_audit_invocation_source")
        batch_op.drop_column("invocation_source_id")
        batch_op.drop_column("invocation_source_kind")
