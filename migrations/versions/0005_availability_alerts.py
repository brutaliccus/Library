"""Availability alerts for notify-when-cached.

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "availability_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("google_volume_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("author", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_availability_alerts_user_id", "availability_alerts", ["user_id"])
    op.create_index(
        "ix_availability_alerts_google_volume_id",
        "availability_alerts",
        ["google_volume_id"],
    )
    op.create_index(
        "ix_availability_alerts_user_volume",
        "availability_alerts",
        ["user_id", "google_volume_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_availability_alerts_user_volume", table_name="availability_alerts")
    op.drop_index("ix_availability_alerts_google_volume_id", table_name="availability_alerts")
    op.drop_index("ix_availability_alerts_user_id", table_name="availability_alerts")
    op.drop_table("availability_alerts")
