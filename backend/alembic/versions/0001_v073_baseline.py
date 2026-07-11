"""Record the schema shipped by v0.7.3.

Revision ID: 0001_v073_baseline
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_v073_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("worktree", sa.String(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "scheduled_task",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("prompt", sa.String(), nullable=False),
        sa.Column("schedule_config", sa.JSON(), nullable=False),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("workspace", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(), nullable=True),
        sa.Column("last_session_id", sa.String(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("loop_max_iterations", sa.Integer(), nullable=True),
        sa.Column("loop_preset", sa.String(), nullable=True),
        sa.Column("loop_stop_marker", sa.String(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_task_next_run_at",
        "scheduled_task",
        ["next_run_at"],
        unique=False,
    )
    op.create_table(
        "workspace_memory",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_path", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_path"),
    )
    op.create_index(
        "ix_workspace_memory_path",
        "workspace_memory",
        ["workspace_path"],
        unique=True,
    )
    op.create_table(
        "session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("directory", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column("summary_additions", sa.Integer(), nullable=True),
        sa.Column("summary_deletions", sa.Integer(), nullable=True),
        sa.Column("summary_files", sa.Integer(), nullable=True),
        sa.Column("summary_diffs", sa.JSON(), nullable=True),
        sa.Column(
            "is_pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("'0'"),
        ),
        sa.Column("permission", sa.JSON(), nullable=True),
        sa.Column("time_compacting", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_archived", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "message",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "part",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_part_session_id", "part", ["session_id"], unique=False)
    op.create_table(
        "todo",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("active_form", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "session_file",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column("file_type", sa.String(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "task_run",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_by", sa.String(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["task_id"], ["scheduled_task.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_run_task_id", "task_run", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_task_run_task_id", table_name="task_run")
    op.drop_table("task_run")
    op.drop_table("session_file")
    op.drop_table("todo")
    op.drop_index("ix_part_session_id", table_name="part")
    op.drop_table("part")
    op.drop_table("message")
    op.drop_table("session")
    op.drop_index("ix_workspace_memory_path", table_name="workspace_memory")
    op.drop_table("workspace_memory")
    op.drop_index("ix_scheduled_task_next_run_at", table_name="scheduled_task")
    op.drop_table("scheduled_task")
    op.drop_table("project")
