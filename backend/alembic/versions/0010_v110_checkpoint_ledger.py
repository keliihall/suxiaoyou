"""Add v1.1 root-turn, checkpoint, and workspace mutation ledgers.

Revision ID: 0010_v110_checkpoint_ledger
Revises: 0009_v100_goal_usage_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0010_v110_checkpoint_ledger"
down_revision = "0009_v100_goal_usage_ledger"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "workspace_instance",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("parent_instance_id", sa.String(), nullable=True),
        sa.Column("created_by_session_id", sa.String(), nullable=True),
        sa.Column(
            "kind", sa.String(length=40), server_default="direct", nullable=False
        ),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("identity_token", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="active", nullable=False
        ),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("time_released", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'released', 'missing')",
            name="ck_workspace_instance_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND time_released IS NULL) OR "
            "(status != 'active' AND time_released IS NOT NULL)",
            name="ck_workspace_instance_release_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_session_id"], ["session.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_instance_id"], ["workspace_instance.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "root_path",
            "identity_token",
            name="uq_workspace_instance_root_identity",
        ),
    )
    op.create_index(
        "ix_workspace_instance_project_status",
        "workspace_instance",
        ["project_id", "status"],
        unique=False,
    )

    op.create_table(
        "turn_run",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("workspace_instance_id", sa.String(), nullable=False),
        sa.Column("root_turn_id", sa.String(), nullable=False),
        sa.Column("parent_turn_id", sa.String(), nullable=True),
        sa.Column("depth", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "source_kind",
            sa.String(length=40),
            server_default="interactive",
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=24), server_default="running", nullable=False
        ),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("request_message_id", sa.String(), nullable=True),
        sa.Column("response_message_id", sa.String(), nullable=True),
        sa.Column("stream_id", sa.String(), nullable=True),
        sa.Column(
            "has_irreversible_side_effects",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("external_side_effects", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("time_started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_finished", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("depth >= 0", name="ck_turn_run_depth"),
        sa.CheckConstraint(
            "(parent_turn_id IS NULL AND root_turn_id = id AND depth = 0) OR "
            "(parent_turn_id IS NOT NULL AND root_turn_id != id AND depth > 0)",
            name="ck_turn_run_root_parent",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled', 'rewound')",
            name="ck_turn_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["parent_turn_id"], ["turn_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["root_turn_id"], ["turn_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["session.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_instance_id"],
            ["workspace_instance.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_turn_run_session_idempotency",
        ),
    )
    op.create_index(
        "ix_turn_run_root_status",
        "turn_run",
        ["root_turn_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_turn_run_session_created",
        "turn_run",
        ["session_id", "time_created"],
        unique=False,
    )

    op.create_table(
        "session_checkpoint",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("workspace_instance_id", sa.String(), nullable=False),
        sa.Column("root_turn_id", sa.String(), nullable=False),
        sa.Column("turn_run_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("anchor_message_id", sa.String(), nullable=True),
        sa.Column("goal_run_id", sa.String(), nullable=True),
        sa.Column("todo_snapshot", sa.JSON(), nullable=False),
        sa.Column("child_turn_ids", sa.JSON(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default="prepared",
            nullable=False,
        ),
        sa.Column(
            "pin_state",
            sa.String(length=24),
            server_default="pinned",
            nullable=False,
        ),
        sa.Column(
            "has_irreversible_side_effects",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("external_side_effects", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("time_finalized", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_rewound", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_pin_released", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_session_checkpoint_sequence"
        ),
        sa.CheckConstraint(
            "state IN ('prepared', 'committing', 'finalized', 'rewinding', "
            "'rewound', 'failed')",
            name="ck_session_checkpoint_state",
        ),
        sa.CheckConstraint(
            "pin_state IN ('pinned', 'released')",
            name="ck_session_checkpoint_pin_state",
        ),
        sa.CheckConstraint(
            "(pin_state = 'pinned' AND time_pin_released IS NULL) OR "
            "(pin_state = 'released' AND time_pin_released IS NOT NULL)",
            name="ck_session_checkpoint_pin_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["root_turn_id"], ["turn_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_run_id"], ["turn_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["session.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_instance_id"],
            ["workspace_instance.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_run_id", name="uq_session_checkpoint_turn_run"),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_session_checkpoint_sequence"
        ),
    )
    op.create_index(
        "ix_session_checkpoint_root_turn",
        "session_checkpoint",
        ["root_turn_id", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_session_checkpoint_session_state",
        "session_checkpoint",
        ["session_id", "state", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_session_checkpoint_workspace_pin",
        "session_checkpoint",
        ["workspace_instance_id", "pin_state"],
        unique=False,
    )

    op.create_table(
        "checkpoint_change",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("turn_run_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column(
            "node_kind",
            sa.String(length=16),
            server_default="file",
            nullable=False,
        ),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("before_exists", sa.Boolean(), nullable=False),
        sa.Column("before_version_id", sa.String(), nullable=True),
        sa.Column("before_sha256", sa.String(length=64), nullable=True),
        sa.Column("before_mode", sa.Integer(), nullable=True),
        sa.Column("after_exists", sa.Boolean(), nullable=False),
        sa.Column("after_sha256", sa.String(length=64), nullable=True),
        sa.Column("after_mode", sa.Integer(), nullable=True),
        sa.Column("after_size", sa.Integer(), nullable=True),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_checkpoint_change_sequence"
        ),
        sa.CheckConstraint(
            "operation IN ('created', 'modified', 'deleted')",
            name="ck_checkpoint_change_operation",
        ),
        sa.CheckConstraint(
            "node_kind IN ('file', 'directory', 'symlink')",
            name="ck_checkpoint_change_node_kind",
        ),
        sa.CheckConstraint(
            "node_kind != 'symlink' OR operation = 'created'",
            name="ck_checkpoint_change_symlink_created_only",
        ),
        sa.CheckConstraint(
            "(operation = 'created' AND before_exists = false AND "
            "after_exists = true) OR "
            "(operation = 'modified' AND before_exists = true AND "
            "after_exists = true) OR "
            "(operation = 'deleted' AND before_exists = true AND "
            "after_exists = false)",
            name="ck_checkpoint_change_existence",
        ),
        sa.CheckConstraint(
            "before_version_id IS NULL OR "
            "(before_exists = true AND node_kind = 'file')",
            name="ck_checkpoint_change_before_version_kind",
        ),
        sa.CheckConstraint(
            "node_kind = 'directory' OR operation = 'created' OR "
            "before_version_id IS NOT NULL",
            name="ck_checkpoint_change_file_restore_source",
        ),
        sa.CheckConstraint(
            "(node_kind = 'directory' AND before_sha256 IS NULL AND "
            "after_sha256 IS NULL) OR "
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
        sa.CheckConstraint(
            "(before_exists = false AND before_mode IS NULL) OR "
            "before_exists = true",
            name="ck_checkpoint_change_before_mode",
        ),
        sa.CheckConstraint(
            "(after_exists = false AND after_mode IS NULL AND "
            "after_size IS NULL) OR after_exists = true",
            name="ck_checkpoint_change_after_metadata",
        ),
        sa.CheckConstraint(
            "before_mode IS NULL OR before_mode >= 0",
            name="ck_checkpoint_change_before_mode_nonnegative",
        ),
        sa.CheckConstraint(
            "after_mode IS NULL OR after_mode >= 0",
            name="ck_checkpoint_change_after_mode_nonnegative",
        ),
        sa.CheckConstraint(
            "after_size IS NULL OR after_size >= 0",
            name="ck_checkpoint_change_after_size_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["checkpoint_id"], ["session_checkpoint.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_run_id"], ["turn_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "checkpoint_id", "sequence", name="uq_checkpoint_change_sequence"
        ),
    )
    op.create_index(
        "ix_checkpoint_change_checkpoint_path",
        "checkpoint_change",
        ["checkpoint_id", "relative_path"],
        unique=False,
    )
    op.create_index(
        "ix_checkpoint_change_turn",
        "checkpoint_change",
        ["turn_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_checkpoint_change_turn", table_name="checkpoint_change")
    op.drop_index(
        "ix_checkpoint_change_checkpoint_path", table_name="checkpoint_change"
    )
    op.drop_table("checkpoint_change")
    op.drop_index(
        "ix_session_checkpoint_workspace_pin", table_name="session_checkpoint"
    )
    op.drop_index(
        "ix_session_checkpoint_session_state", table_name="session_checkpoint"
    )
    op.drop_index(
        "ix_session_checkpoint_root_turn", table_name="session_checkpoint"
    )
    op.drop_table("session_checkpoint")
    op.drop_index("ix_turn_run_session_created", table_name="turn_run")
    op.drop_index("ix_turn_run_root_status", table_name="turn_run")
    op.drop_table("turn_run")
    op.drop_index(
        "ix_workspace_instance_project_status", table_name="workspace_instance"
    )
    op.drop_table("workspace_instance")
