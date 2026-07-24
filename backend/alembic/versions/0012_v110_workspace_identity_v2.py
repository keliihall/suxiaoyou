"""Mark the workspace identity protocol v2 release boundary.

Revision ID: 0012_v110_workspace_identity_v2
Revises: 0011_v110_user_office_templates

Workspace identity v2 is a runtime/storage-protocol change rather than a
relational schema change.  This deliberately empty revision gives releases a
stable compatibility boundary without performing data or filesystem migration
from inside Alembic.
"""

from __future__ import annotations


revision = "0012_v110_workspace_identity_v2"
down_revision = "0011_v110_user_office_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record the protocol boundary without mutating schema or data."""

    pass


def downgrade() -> None:
    """Remove only the Alembic revision marker change."""

    pass
