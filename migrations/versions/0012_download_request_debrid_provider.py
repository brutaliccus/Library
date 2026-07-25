"""Add download_requests.debrid_provider for TorBox/RD selection.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("download_requests") as batch:
        batch.add_column(
            sa.Column("debrid_provider", sa.String(length=16), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("download_requests") as batch:
        batch.drop_column("debrid_provider")
