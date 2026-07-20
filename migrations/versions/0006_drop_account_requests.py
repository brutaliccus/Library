"""Drop account_requests — signup is invite-only now.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "account_requests" not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("account_requests")}
    if "ix_account_requests_token" in indexes:
        op.drop_index("ix_account_requests_token", table_name="account_requests")
    op.drop_table("account_requests")


def downgrade() -> None:
    op.create_table(
        "account_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("deny_reason", sa.Text(), nullable=True),
        sa.Column("temp_password", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_account_requests_token", "account_requests", ["token"], unique=True)
