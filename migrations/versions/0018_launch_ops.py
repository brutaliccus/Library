"""Launch ops: share ebook fields, source path, invite rotation, admin IP.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    user_cols = {row[1] for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()}
    with op.batch_alter_table("users") as batch:
        if "last_client_ip" not in user_cols:
            batch.add_column(sa.Column("last_client_ip", sa.String(length=64), nullable=True))

    req_cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(download_requests)")).fetchall()
    }
    with op.batch_alter_table("download_requests") as batch:
        if "source_library_path" not in req_cols:
            batch.add_column(sa.Column("source_library_path", sa.String(length=1024), nullable=True))

    grp_cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(library_groups)")).fetchall()
    }
    with op.batch_alter_table("library_groups") as batch:
        if "invite_rotated_at" not in grp_cols:
            batch.add_column(sa.Column("invite_rotated_at", sa.DateTime(timezone=True), nullable=True))

    share_cols = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(book_shares)")).fetchall()
    }
    with op.batch_alter_table("book_shares") as batch:
        if "media_type" not in share_cols:
            batch.add_column(
                sa.Column("media_type", sa.String(length=16), nullable=False, server_default="audiobook")
            )
        if "kavita_series_id" not in share_cols:
            batch.add_column(sa.Column("kavita_series_id", sa.Integer(), nullable=True))
        if "kavita_chapter_id" not in share_cols:
            batch.add_column(sa.Column("kavita_chapter_id", sa.Integer(), nullable=True))
        if "title" not in share_cols:
            batch.add_column(sa.Column("title", sa.String(length=512), nullable=True))
        # Ebooks have no ABS item; allow null for audiobook-only column.
        batch.alter_column("abs_item_id", existing_type=sa.String(length=128), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("book_shares") as batch:
        batch.drop_column("title")
        batch.drop_column("kavita_chapter_id")
        batch.drop_column("kavita_series_id")
        batch.drop_column("media_type")
    with op.batch_alter_table("library_groups") as batch:
        batch.drop_column("invite_rotated_at")
    with op.batch_alter_table("download_requests") as batch:
        batch.drop_column("source_library_path")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_client_ip")
