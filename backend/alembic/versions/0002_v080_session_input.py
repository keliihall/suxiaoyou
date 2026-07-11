"""Add persistent queued and steering inputs for v0.8.0.

Revision ID: 0002_v080_session_input
Revises: 0001_v073_baseline
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_v080_session_input"
down_revision = "0001_v073_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_input",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("workspace", sa.String(), nullable=True),
        sa.Column("reasoning", sa.Boolean(), nullable=True),
        sa.Column("permission_presets", sa.JSON(), nullable=True),
        sa.Column("permission_rules", sa.JSON(), nullable=True),
        sa.Column("target_stream_id", sa.String(), nullable=True),
        sa.Column("applied_stream_id", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("time_applied", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_session_input_client_request",
        ),
    )
    op.create_index(
        "ix_session_input_dispatch",
        "session_input",
        ["session_id", "status", "position"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_session_input_dispatch", table_name="session_input")
    op.drop_table("session_input")
