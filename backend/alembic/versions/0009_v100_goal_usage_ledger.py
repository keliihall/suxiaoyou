"""Add idempotent per-run Goal usage records.

Revision ID: 0009_v100_goal_usage_ledger
Revises: 0008_v100_session_goal
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009_v100_goal_usage_ledger"
down_revision = "0008_v100_session_goal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goal_usage_record",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("goal_run_id", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_used_microusd",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
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
        sa.CheckConstraint("tokens_used >= 0", name="ck_goal_usage_tokens"),
        sa.CheckConstraint(
            "cost_used_microusd >= 0",
            name="ck_goal_usage_cost",
        ),
        sa.ForeignKeyConstraint(
            ["goal_run_id"],
            ["goal_run.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "goal_run_id",
            "source_key",
            name="uq_goal_usage_run_source",
        ),
    )
    op.create_index(
        "ix_goal_usage_run",
        "goal_usage_record",
        ["goal_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_goal_usage_run", table_name="goal_usage_record")
    op.drop_table("goal_usage_record")
