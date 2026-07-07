"""One-shot scraper health check — run inside the app container."""
import asyncio
import sys

from sqlalchemy import select, func, desc

from app.database import async_session
from app.models import IndexerTorrent, CatalogTorrentMatch, ScraperState
from app.services.indexer_scraper import get_status, _run_scrape_job


async def main() -> int:
    try:
        status = await get_status()
    except Exception as e:
        print(f"FAIL: get_status raised: {e}")
        return 1

    print("=== scraper status ===")
    for k, v in status.items():
        print(f"  {k}: {v}")

    async with async_session() as db:
        torrents = await db.scalar(select(func.count()).select_from(IndexerTorrent))
        active = await db.scalar(
            select(func.count()).select_from(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
        )
        matches = await db.scalar(select(func.count()).select_from(CatalogTorrentMatch))
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one_or_none()
        recent = (
            await db.execute(
                select(IndexerTorrent.title, IndexerTorrent.indexer, IndexerTorrent.first_seen_at)
                .order_by(desc(IndexerTorrent.first_seen_at))
                .limit(5)
            )
        ).all()

    print("\n=== database ===")
    print(f"  indexer_torrents total: {torrents}")
    print(f"  indexer_torrents active: {active}")
    print(f"  catalog_torrent_matches: {matches}")
    if state:
        print(f"  scraper_state.enabled: {state.enabled}")
        print(f"  scraper_state.status: {state.status}")
        print(f"  scraper_state.last_error: {state.last_error}")

    if recent:
        print("\n=== recent torrents ===")
        for title, indexer, seen in recent:
            print(f"  [{indexer}] {title[:80]!r} @ {seen}")

    if "--run-job" in sys.argv:
        print("\n=== running one scrape job now ===")
        await _run_scrape_job()
        status2 = await get_status()
        print("post-job status:", status2)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
