"""Per-user OPDS token + ereader shelf items.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    user_cols = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    with op.batch_alter_table("users") as batch:
        if "opds_token" not in user_cols:
            batch.add_column(sa.Column("opds_token", sa.String(length=64), nullable=True))

    # Unique index (nullable tokens allowed for users who never connected)
    indexes = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA index_list(users)")).fetchall()
    }
    if "ix_users_opds_token" not in indexes:
        op.create_index("ix_users_opds_token", "users", ["opds_token"], unique=True)

    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "user_ereader_items" not in tables:
        op.create_table(
            "user_ereader_items",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("kavita_series_id", sa.Integer(), nullable=False),
            sa.Column("kavita_chapter_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("author", sa.String(length=256), nullable=False, server_default=""),
            sa.Column("cover_url", sa.String(length=1024), nullable=False, server_default=""),
            sa.Column(
                "added_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_user_ereader_items_user_id",
            "user_ereader_items",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_user_ereader_items_chapter",
            "user_ereader_items",
            ["kavita_chapter_id"],
            unique=False,
        )
        op.create_index(
            "uq_user_ereader_user_chapter",
            "user_ereader_items",
            ["user_id", "kavita_chapter_id"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index("uq_user_ereader_user_chapter", table_name="user_ereader_items")
    op.drop_index("ix_user_ereader_items_chapter", table_name="user_ereader_items")
    op.drop_index("ix_user_ereader_items_user_id", table_name="user_ereader_items")
    op.drop_table("user_ereader_items")
    op.drop_index("ix_users_opds_token", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("opds_token")
