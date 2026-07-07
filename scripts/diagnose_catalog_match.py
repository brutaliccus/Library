"""Diagnose catalog matching on Pi."""
import asyncio
from sqlalchemy import select, func

from app.database import async_session
from app.models import IndexerTorrent, CatalogTorrentMatch
from app.services import catalog_match, google_books


async def main():
    async with async_session() as db:
        media = dict(
            (await db.execute(
                select(IndexerTorrent.media_type, func.count())
                .group_by(IndexerTorrent.media_type)
            )).all()
        )
        print("media_types:", media)

        book_torrents = (
            await db.execute(
                select(IndexerTorrent.title, IndexerTorrent.media_type, IndexerTorrent.indexer)
                .where(IndexerTorrent.media_type.in_(("audiobook", "ebook")))
                .limit(15)
            )
        ).all()
        print(f"\nbook torrents ({len(book_torrents)} shown):")
        for t in book_torrents:
            print(f"  [{t[2]}] {t[1]}: {t[0][:90]}")

    matches_before = (
        await db.scalar(select(func.count()).select_from(CatalogTorrentMatch))
    )
    print(f"\ncatalog_matches before: {matches_before}")

    # Try running match batch
    created = await catalog_match.run_match_batch(100)
    print(f"match_batch touched: {created}")

    async with async_session() as db:
        matches_after = await db.scalar(select(func.count()).select_from(CatalogTorrentMatch))
        tiers = dict(
            (await db.execute(
                select(CatalogTorrentMatch.match_tier, func.count())
                .group_by(CatalogTorrentMatch.match_tier)
            )).all()
        )
    print(f"catalog_matches after: {matches_after}, tiers: {tiers}")

    # Try matching trending volumes against cache
    trending = await google_books.get_trending(max_results=5)
    print(f"\ntrending volumes: {len(trending)}")
    for b in trending[:3]:
        vid = b.get("volumeId") or b.get("id")
        title = b.get("title", "")
        author = (b.get("authors") or [""])[0]
        n = await catalog_match.match_volume_to_torrents(vid, title, author)
        print(f"  match_volume_to_torrents({title!r}): {n}")


if __name__ == "__main__":
    asyncio.run(main())
