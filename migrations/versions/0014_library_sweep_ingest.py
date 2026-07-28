"""Library Sweep ingest fields + library_sweep_jobs table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dr_cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(download_requests)")).fetchall()
    }
    with op.batch_alter_table("download_requests") as batch:
        if "source" not in dr_cols:
            batch.add_column(sa.Column("source", sa.String(length=16), nullable=True))
        if "abs_item_id" not in dr_cols:
            batch.add_column(sa.Column("abs_item_id", sa.String(length=128), nullable=True))
        if "ingest_fingerprint" not in dr_cols:
            batch.add_column(
                sa.Column("ingest_fingerprint", sa.String(length=256), nullable=True)
            )

    # Indexes (idempotent via try / IF NOT EXISTS for SQLite)
    existing_indexes = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA index_list(download_requests)")).fetchall()
    }
    if "ix_download_requests_abs_item_id" not in existing_indexes:
        op.create_index(
            "ix_download_requests_abs_item_id",
            "download_requests",
            ["abs_item_id"],
        )
    if "ix_download_requests_ingest_fingerprint" not in existing_indexes:
        op.create_index(
            "ix_download_requests_ingest_fingerprint",
            "download_requests",
            ["ingest_fingerprint"],
        )

    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "library_sweep_jobs" not in tables:
        op.create_table(
            "library_sweep_jobs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="idle"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("scanned", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("auto_applied", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("needs_review", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("m4b_queued", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("review_cursor_request_id", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_by_user_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "library_sweep_jobs" in tables:
        op.drop_table("library_sweep_jobs")

    existing_indexes = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA index_list(download_requests)")).fetchall()
    }
    if "ix_download_requests_ingest_fingerprint" in existing_indexes:
        op.drop_index(
            "ix_download_requests_ingest_fingerprint",
            table_name="download_requests",
        )
    if "ix_download_requests_abs_item_id" in existing_indexes:
        op.drop_index("ix_download_requests_abs_item_id", table_name="download_requests")

    dr_cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(download_requests)")).fetchall()
    }
    with op.batch_alter_table("download_requests") as batch:
        if "ingest_fingerprint" in dr_cols:
            batch.drop_column("ingest_fingerprint")
        if "abs_item_id" in dr_cols:
            batch.drop_column("abs_item_id")
        if "source" in dr_cols:
            batch.drop_column("source")
