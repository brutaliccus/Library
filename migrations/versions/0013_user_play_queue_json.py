"""Add users.play_queue_json for synced Up Next queue.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    if "play_queue_json" in cols:
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("play_queue_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    if "play_queue_json" not in cols:
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column("play_queue_json")
