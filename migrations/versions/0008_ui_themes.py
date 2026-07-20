"""UI theme ids shared by library default + user preference.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("library_groups") as batch:
        batch.add_column(
            sa.Column("default_theme", sa.String(length=32), nullable=False, server_default="ocean")
        )
    with op.batch_alter_table("users") as batch:
        # NULL = follow the library's default_theme
        batch.add_column(sa.Column("theme", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("theme")
    with op.batch_alter_table("library_groups") as batch:
        batch.drop_column("default_theme")
