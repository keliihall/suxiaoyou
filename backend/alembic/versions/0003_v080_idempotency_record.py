"""Add the durable request idempotency ledger for v0.8.0.

Revision ID: 0003_v080_idempotency_record
Revises: 0002_v080_session_input
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_v080_idempotency_record"
down_revision = "0002_v080_session_input"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_record",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "time_created",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "time_updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "request_key",
            name="uq_idempotency_scope_key",
        ),
    )
    op.create_index(
        "ix_idempotency_status",
        "idempotency_record",
        ["scope", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_status", table_name="idempotency_record")
    op.drop_table("idempotency_record")
