"""Track RSS/recent-feed ingestion runs on scraper_state.

Revision ID: 0003
Revises: 0002

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scraper_state", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_rss_run_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("last_rss_upserted", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("scraper_state", schema=None) as batch_op:
        batch_op.drop_column("last_rss_upserted")
        batch_op.drop_column("last_rss_run_at")
