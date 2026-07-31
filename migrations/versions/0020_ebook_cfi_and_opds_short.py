"""Store EPUB CFI on reading progress + short OPDS codes.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    progress_cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(ebook_reading_progress)")).fetchall()
    }
    if "cfi" not in progress_cols:
        with op.batch_alter_table("ebook_reading_progress") as batch:
            batch.add_column(sa.Column("cfi", sa.Text(), nullable=True))

    user_cols = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    if "opds_short_code" not in user_cols:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("opds_short_code", sa.String(length=16), nullable=True))

    indexes = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA index_list(users)")).fetchall()
    }
    if "ix_users_opds_short_code" not in indexes:
        op.create_index(
            "ix_users_opds_short_code",
            "users",
            ["opds_short_code"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index("ix_users_opds_short_code", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("opds_short_code")
    with op.batch_alter_table("ebook_reading_progress") as batch:
        batch.drop_column("cfi")
