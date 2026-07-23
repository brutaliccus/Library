"""LibraForge pipeline fields on download requests.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("download_requests") as batch:
        batch.add_column(sa.Column("staging_path", sa.String(length=1024), nullable=True))
        batch.add_column(sa.Column("libraforge_run_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("quarantine_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("download_requests") as batch:
        batch.drop_column("quarantine_reason")
        batch.drop_column("libraforge_run_id")
        batch.drop_column("staging_path")
