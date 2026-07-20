"""Store catalog id + cover on download requests for Requests UI navigation.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("download_requests") as batch:
        batch.add_column(sa.Column("google_volume_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("cover_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("download_requests") as batch:
        batch.drop_column("cover_url")
        batch.drop_column("google_volume_id")
