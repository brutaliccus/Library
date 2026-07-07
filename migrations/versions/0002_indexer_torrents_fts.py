"""Full-text search index for the torrent cache.

Creates an FTS5 virtual table over indexer_torrents(title_norm, author_norm)
kept in sync by triggers, so book search stays fast as the cache grows past
100k rows (the previous LIKE '%...%' query was a full table scan).

Revision ID: 0002
Revises: 0001

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS indexer_torrents_fts USING fts5(
            title_norm,
            author_norm,
            content='indexer_torrents',
            content_rowid='id'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS indexer_torrents_fts_ai
        AFTER INSERT ON indexer_torrents BEGIN
            INSERT INTO indexer_torrents_fts(rowid, title_norm, author_norm)
            VALUES (new.id, new.title_norm, new.author_norm);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS indexer_torrents_fts_ad
        AFTER DELETE ON indexer_torrents BEGIN
            INSERT INTO indexer_torrents_fts(indexer_torrents_fts, rowid, title_norm, author_norm)
            VALUES ('delete', old.id, old.title_norm, old.author_norm);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS indexer_torrents_fts_au
        AFTER UPDATE OF title_norm, author_norm ON indexer_torrents BEGIN
            INSERT INTO indexer_torrents_fts(indexer_torrents_fts, rowid, title_norm, author_norm)
            VALUES ('delete', old.id, old.title_norm, old.author_norm);
            INSERT INTO indexer_torrents_fts(rowid, title_norm, author_norm)
            VALUES (new.id, new.title_norm, new.author_norm);
        END
        """
    )
    # Backfill the index from existing rows.
    op.execute("INSERT INTO indexer_torrents_fts(indexer_torrents_fts) VALUES ('rebuild')")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS indexer_torrents_fts_au")
    op.execute("DROP TRIGGER IF EXISTS indexer_torrents_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS indexer_torrents_fts_ai")
    op.execute("DROP TABLE IF EXISTS indexer_torrents_fts")
