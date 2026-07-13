"""Snapshot UI language for each queued session input.

Revision ID: 0004_v083_session_input_language
Revises: 0003_v080_idempotency_record
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_v083_session_input_language"
down_revision = "0003_v080_idempotency_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_input",
        sa.Column("language", sa.String(), nullable=False, server_default="zh"),
    )


def downgrade() -> None:
    op.drop_column("session_input", "language")
