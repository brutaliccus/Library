"""Library Sweep job medium (audiobook | ebook).

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(library_sweep_jobs)")).fetchall()
    }
    if "medium" not in cols:
        with op.batch_alter_table("library_sweep_jobs") as batch:
            batch.add_column(
                sa.Column(
                    "medium",
                    sa.String(length=16),
                    nullable=False,
                    server_default="audiobook",
                )
            )

    existing_indexes = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA index_list(library_sweep_jobs)")).fetchall()
    }
    if "ix_library_sweep_jobs_medium" not in existing_indexes:
        op.create_index(
            "ix_library_sweep_jobs_medium",
            "library_sweep_jobs",
            ["medium"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing_indexes = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA index_list(library_sweep_jobs)")).fetchall()
    }
    if "ix_library_sweep_jobs_medium" in existing_indexes:
        op.drop_index("ix_library_sweep_jobs_medium", table_name="library_sweep_jobs")

    cols = {
        row[1]
        for row in bind.execute(sa.text("PRAGMA table_info(library_sweep_jobs)")).fetchall()
    }
    if "medium" in cols:
        with op.batch_alter_table("library_sweep_jobs") as batch:
            batch.drop_column("medium")
