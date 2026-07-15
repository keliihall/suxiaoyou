"""Add the persistent Goal control plane and execution ledger.

Revision ID: 0008_v100_session_goal
Revises: 0007_v100_invocation_source
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008_v100_session_goal"
down_revision = "0007_v100_invocation_source"
branch_labels = None
depends_on = None


_ACTIVE_RUN_SQL = "status IN ('reserved', 'running', 'waiting_user')"


def upgrade() -> None:
    op.create_table(
        "session_goal",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("definition_of_done", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default="active", nullable=False
        ),
        sa.Column(
            "run_state", sa.String(length=32), server_default="idle", nullable=False
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("token_budget", sa.Integer(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_budget_microusd", sa.Integer(), nullable=True),
        sa.Column(
            "cost_used_microusd", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("time_budget_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "time_used_seconds", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("max_continuations", sa.Integer(), nullable=True),
        sa.Column(
            "continuation_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "no_progress_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("blocker_streak", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "consecutive_error_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("blocker_code", sa.String(length=80), nullable=True),
        sa.Column("blocker_message", sa.Text(), nullable=True),
        sa.Column("needs_review", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_summary", sa.Text(), nullable=True),
        sa.Column("completion_evidence", sa.JSON(), nullable=True),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column(
            "agent", sa.String(length=80), server_default="build", nullable=False
        ),
        sa.Column("reasoning", sa.Boolean(), nullable=True),
        sa.Column(
            "language", sa.String(length=8), server_default="zh", nullable=False
        ),
        sa.Column("permission_snapshot", sa.JSON(), nullable=True),
        sa.Column("last_run_id", sa.String(), nullable=True),
        sa.Column("last_stream_id", sa.String(), nullable=True),
        sa.Column("time_started", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_completed", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('active', 'paused', 'blocked', 'usage_limited', "
            "'budget_limited', 'complete')",
            name="ck_session_goal_status",
        ),
        sa.CheckConstraint(
            "run_state IN ('idle', 'reserved', 'running', 'pausing', "
            "'waiting_user', 'interrupted')",
            name="ck_session_goal_run_state",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_session_goal_revision"),
        sa.CheckConstraint(
            "length(objective) + length(coalesce(definition_of_done, '')) <= 4000",
            name="ck_session_goal_content_length",
        ),
        sa.CheckConstraint(
            "token_budget IS NULL OR token_budget >= 0",
            name="ck_session_goal_token_budget",
        ),
        sa.CheckConstraint("tokens_used >= 0", name="ck_session_goal_tokens_used"),
        sa.CheckConstraint(
            "cost_budget_microusd IS NULL OR cost_budget_microusd >= 0",
            name="ck_session_goal_cost_budget",
        ),
        sa.CheckConstraint(
            "cost_used_microusd >= 0", name="ck_session_goal_cost_used"
        ),
        sa.CheckConstraint(
            "time_budget_seconds IS NULL OR time_budget_seconds >= 0",
            name="ck_session_goal_time_budget",
        ),
        sa.CheckConstraint(
            "time_used_seconds >= 0", name="ck_session_goal_time_used"
        ),
        sa.CheckConstraint(
            "max_continuations IS NULL OR max_continuations >= 0",
            name="ck_session_goal_max_continuations",
        ),
        sa.CheckConstraint(
            "continuation_count >= 0", name="ck_session_goal_continuation_count"
        ),
        sa.CheckConstraint(
            "no_progress_count >= 0", name="ck_session_goal_no_progress_count"
        ),
        sa.CheckConstraint(
            "blocker_streak >= 0", name="ck_session_goal_blocker_streak"
        ),
        sa.CheckConstraint(
            "consecutive_error_count >= 0",
            name="ck_session_goal_consecutive_error_count",
        ),
        sa.CheckConstraint(
            "status != 'complete' OR (completion_summary IS NOT NULL "
            "AND completion_evidence IS NOT NULL AND time_completed IS NOT NULL)",
            name="ck_session_goal_completion_contract",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "session_id", name="uq_session_goal_id_session"
        ),
        sa.UniqueConstraint("session_id", name="uq_session_goal_session"),
    )

    with op.batch_alter_table("todo") as batch_op:
        batch_op.add_column(sa.Column("goal_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_todo_goal_session",
            "session_goal",
            ["goal_id", "session_id"],
            ["id", "session_id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_todo_goal", ["goal_id"], unique=False)

    op.create_table(
        "goal_run",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("goal_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("goal_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("stream_id", sa.String(), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="reserved", nullable=False
        ),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_used_microusd", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("active_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress_summary", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.String(length=160), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "side_effects_started", sa.Boolean(), server_default="0", nullable=False
        ),
        sa.Column("time_started", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_finished", sa.DateTime(timezone=True), nullable=True),
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
            "trigger IN ('initial', 'auto', 'resume', 'user_input')",
            name="ck_goal_run_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'running', 'waiting_user', 'completed', "
            "'blocked', 'interrupted', 'failed')",
            name="ck_goal_run_status",
        ),
        sa.CheckConstraint("ordinal >= 1", name="ck_goal_run_ordinal"),
        sa.CheckConstraint("goal_revision >= 1", name="ck_goal_run_revision"),
        sa.CheckConstraint("tokens_used >= 0", name="ck_goal_run_tokens_used"),
        sa.CheckConstraint(
            "cost_used_microusd >= 0", name="ck_goal_run_cost_used"
        ),
        sa.CheckConstraint(
            "active_seconds >= 0", name="ck_goal_run_active_seconds"
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["session_goal.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("goal_id", "ordinal", name="uq_goal_run_ordinal"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_goal_run_idempotency_key"
        ),
    )
    op.create_index(
        "ix_goal_run_goal_status", "goal_run", ["goal_id", "status"], unique=False
    )
    op.create_index(
        "uq_goal_run_one_active",
        "goal_run",
        ["goal_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_RUN_SQL),
        postgresql_where=sa.text(_ACTIVE_RUN_SQL),
    )


def downgrade() -> None:
    op.drop_index("uq_goal_run_one_active", table_name="goal_run")
    op.drop_index("ix_goal_run_goal_status", table_name="goal_run")
    op.drop_table("goal_run")
    with op.batch_alter_table("todo") as batch_op:
        batch_op.drop_index("ix_todo_goal")
        batch_op.drop_constraint(
            "fk_todo_goal_session", type_="foreignkey"
        )
        batch_op.drop_column("goal_id")
    op.drop_table("session_goal")
