"""DownloadRequest.release_files_json for ABB / debrid file lists.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(download_requests)")).fetchall()
    }
    if "release_files_json" not in cols:
        with op.batch_alter_table("download_requests") as batch:
            batch.add_column(sa.Column("release_files_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(download_requests)")).fetchall()
    }
    if "release_files_json" in cols:
        with op.batch_alter_table("download_requests") as batch:
            batch.drop_column("release_files_json")
