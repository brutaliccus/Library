"""Book share links + per-user can_share_books flag.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    user_cols = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    with op.batch_alter_table("users") as batch:
        if "can_share_books" not in user_cols:
            batch.add_column(
                sa.Column(
                    "can_share_books",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )

    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "book_shares" not in tables:
        op.create_table(
            "book_shares",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("token", sa.String(length=64), nullable=False),
            sa.Column("abs_item_id", sa.String(length=128), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_book_shares_token", "book_shares", ["token"], unique=True)
        op.create_index("ix_book_shares_abs_item_id", "book_shares", ["abs_item_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_book_shares_abs_item_id", table_name="book_shares")
    op.drop_index("ix_book_shares_token", table_name="book_shares")
    op.drop_table("book_shares")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("can_share_books")
