"""Add user email and library group cover art.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("email", sa.String(length=255), nullable=True))
    # Unique index allows multiple NULLs on SQLite.
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    with op.batch_alter_table("library_groups") as batch:
        batch.add_column(sa.Column("cover_path", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_groups") as batch:
        batch.drop_column("cover_path")
    op.drop_index("ix_users_email", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email")
