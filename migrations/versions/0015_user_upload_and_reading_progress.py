"""Per-user audiobook upload flag + ebook reading progress sync.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    user_cols = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    with op.batch_alter_table("users") as batch:
        if "allow_audiobook_upload" not in user_cols:
            batch.add_column(
                sa.Column(
                    "allow_audiobook_upload",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )

    # One-time migrate: if the global toggle was on, enable upload for all users.
    try:
        row = bind.execute(
            sa.text(
                "SELECT value FROM instance_settings WHERE key = 'allow_user_audiobook_upload'"
            )
        ).fetchone()
        global_on = False
        if row and row[0] is not None:
            global_on = str(row[0]).strip().lower() in ("1", "true", "yes", "on")
        if global_on:
            bind.execute(sa.text("UPDATE users SET allow_audiobook_upload = 1"))
    except Exception:
        pass

    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "ebook_reading_progress" not in tables:
        op.create_table(
            "ebook_reading_progress",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("chapter_id", sa.Integer(), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("viewport_page", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_viewport_pages", sa.Integer(), nullable=True),
            sa.Column("total_kavita_pages", sa.Integer(), nullable=True),
            sa.Column("book_title", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("series_name", sa.String(length=512), nullable=True),
            sa.Column("cover_url", sa.String(length=1024), nullable=False, server_default=""),
            sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "chapter_id", name="uq_ebook_reading_progress_user_chapter"
            ),
        )
        op.create_index(
            "ix_ebook_reading_progress_user_id",
            "ebook_reading_progress",
            ["user_id"],
        )
        op.create_index(
            "ix_ebook_reading_progress_chapter_id",
            "ebook_reading_progress",
            ["chapter_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "ebook_reading_progress" in tables:
        op.drop_index("ix_ebook_reading_progress_chapter_id", table_name="ebook_reading_progress")
        op.drop_index("ix_ebook_reading_progress_user_id", table_name="ebook_reading_progress")
        op.drop_table("ebook_reading_progress")

    user_cols = {
        row[1] for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    if "allow_audiobook_upload" in user_cols:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("allow_audiobook_upload")
