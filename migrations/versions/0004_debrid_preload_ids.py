"""Store debrid account torrent ids on indexer rows.

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("indexer_torrents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rd_debrid_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("torbox_debrid_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("rd_preloaded_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("torbox_preloaded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("indexer_torrents", schema=None) as batch_op:
        batch_op.drop_column("torbox_preloaded_at")
        batch_op.drop_column("rd_preloaded_at")
        batch_op.drop_column("torbox_debrid_id")
        batch_op.drop_column("rd_debrid_id")
