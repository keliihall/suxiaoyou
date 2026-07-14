"""Establish the formal v0.9.0 release boundary.

Revision ID: 0005_v090_release_boundary
Revises: 0004_v083_session_input_language

This revision adds the server-owned effective permission snapshot used as the
hard ceiling for non-interactive child tasks.  Keeping it separate from the
legacy public ``session.permission`` field prevents request data from forging
the delegation boundary.  The revision also provides a stable boundary for
backup/restore compatibility checks.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_v090_release_boundary"
down_revision = "0004_v083_session_input_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session",
        sa.Column("permission_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session", "permission_snapshot")
