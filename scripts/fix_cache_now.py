"""Prune junk and run one book-focused scrape on Pi."""
import asyncio

from sqlalchemy import select, func

from app.database import async_session
from app.models import IndexerTorrent, CatalogTorrentMatch
from app.services import indexer_cache, indexer_scraper, catalog_match, prowlarr


async def main():
    pruned = await indexer_cache.prune_non_book_torrents()
    print(f"pruned: {pruned}")

    async with async_session() as db:
        active = await db.scalar(
            select(func.count()).select_from(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
        )
        print(f"active torrents after prune: {active}")

    results = await prowlarr.search_trusted_indexers_multi(["fantasy audiobook"])
    print(f"prowlarr results: {len(results)}")
    if results[:3]:
        for r in results[:3]:
            print(f"  - [{r.get('indexer')}] {r.get('title','')[:80]}")

    upserted = await indexer_cache.upsert_torrents(results)
    pruned2 = await indexer_cache.prune_non_book_torrents()
    matches = await catalog_match.run_match_batch(100)
    print(f"upserted: {upserted}, pruned again: {pruned2}, match rows: {matches}")

    async with async_session() as db:
        active = await db.scalar(
            select(func.count()).select_from(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
        )
        m = await db.scalar(select(func.count()).select_from(CatalogTorrentMatch))
        abb = (
            await db.execute(
                select(IndexerTorrent.title, IndexerTorrent.indexer)
                .where(IndexerTorrent.is_active.is_(True))
                .limit(8)
            )
        ).all()
    print(f"active: {active}, catalog_matches: {m}")
    for t in abb:
        print(f"  [{t[1]}] {t[0][:75]}")


if __name__ == "__main__":
    asyncio.run(main())
