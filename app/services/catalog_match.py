"""Link cached torrents to Google Books catalog volumes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import IndexerTorrent, CatalogTorrentMatch, SearchHistory
from app.services import google_books
from app.services.download_discovery import (
    BookSearchContext,
    resolve_book_search_context,
    score_torrent_title,
)
from app.services.indexer_cache import extract_isbn, torrent_row_to_api

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isbn_from_volume(volume: dict) -> set[str]:
    ids: set[str] = set()
    for ident in volume.get("industryIdentifiers") or []:
        raw = (ident.get("identifier") or "").strip()
        digits = "".join(c for c in raw.upper() if c.isdigit() or c == "X")
        if len(digits) in (10, 13):
            ids.add(digits)
    return ids


async def _candidate_volumes(limit: int = 40) -> list[dict]:
    """Bounded catalog volumes to match against new torrents."""
    volumes: list[dict] = []
    seen: set[str] = set()

    try:
        trending = await google_books.get_trending(max_results=15)
        for b in trending:
            vid = b.get("volumeId") or b.get("id")
            if vid and vid not in seen:
                seen.add(vid)
                volumes.append(b)
    except Exception as e:
        logger.debug("catalog_match trending failed: %s", e)

    async with async_session() as db:
        hist = (
            await db.execute(
                select(SearchHistory.query)
                .order_by(SearchHistory.created_at.desc())
                .limit(20)
            )
        ).scalars().all()

    for q in hist:
        if len(volumes) >= limit:
            break
        try:
            result = await google_books.search_volumes(q, max_results=5)
            for b in result.get("books") or []:
                vid = b.get("volumeId") or b.get("id")
                if vid and vid not in seen:
                    seen.add(vid)
                    volumes.append(b)
        except Exception:
            continue

    return volumes[:limit]


async def match_torrent_to_volumes(
    torrent: IndexerTorrent,
    volumes: list[dict],
    db: AsyncSession,
) -> int:
    """Create catalog_torrent_matches for one torrent. Returns match count."""
    created = 0
    api_row = torrent_row_to_api(torrent)
    torrent_isbn = torrent.parsed_isbn

    for vol in volumes:
        volume_id = vol.get("volumeId") or vol.get("id")
        if not volume_id:
            continue
        title = vol.get("title") or ""
        authors = vol.get("authors") or []
        author = authors[0] if authors else ""
        ctx = resolve_book_search_context(title=title, author=author)

        match_method = "fuzzy"
        tier = "weak"
        score = 0.0

        if torrent_isbn:
            vol_isbns = _isbn_from_volume(vol)
            if torrent_isbn in vol_isbns:
                match_method = "isbn"
                tier = "exact"
                score = 100.0

        if match_method != "isbn":
            score, tier = score_torrent_title(torrent.title, ctx)
            if tier == "weak" and score < 25:
                continue

        existing = (
            await db.execute(
                select(CatalogTorrentMatch).where(
                    CatalogTorrentMatch.google_volume_id == volume_id,
                    CatalogTorrentMatch.info_hash == torrent.info_hash,
                )
            )
        ).scalar_one_or_none()

        if existing:
            existing.match_method = match_method
            existing.match_tier = tier
            existing.score = score
            existing.updated_at = _utcnow()
        else:
            db.add(
                CatalogTorrentMatch(
                    google_volume_id=volume_id,
                    info_hash=torrent.info_hash,
                    match_method=match_method,
                    match_tier=tier,
                    score=score,
                )
            )
        created += 1

    return created


async def match_volume_to_torrents(
    volume_id: str,
    title: str,
    author: str = "",
    isbns: list[str] | None = None,
    db: AsyncSession | None = None,
) -> int:
    """Match a single catalog volume against active cached torrents."""
    ctx = resolve_book_search_context(title=title, author=author)
    isbn_set = set(isbns or [])

    async def _do(session: AsyncSession) -> int:
        from app.services.indexer_cache import _normalize_text
        base = _normalize_text(ctx.base_title)[:40]
        q = select(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
        if base:
            q = q.where(IndexerTorrent.title_norm.contains(base))
        torrents = (await session.execute(q.limit(300))).scalars().all()
        count = 0
        for t in torrents:
            if isbn_set and t.parsed_isbn and t.parsed_isbn in isbn_set:
                tier, method, score = "exact", "isbn", 100.0
            else:
                score, tier = score_torrent_title(t.title, ctx)
                method = "fuzzy"
                if tier == "weak" and score < 25:
                    continue
            existing = (
                await session.execute(
                    select(CatalogTorrentMatch).where(
                        CatalogTorrentMatch.google_volume_id == volume_id,
                        CatalogTorrentMatch.info_hash == t.info_hash,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                existing.match_method = method
                existing.match_tier = tier
                existing.score = score
                existing.updated_at = _utcnow()
            else:
                session.add(
                    CatalogTorrentMatch(
                        google_volume_id=volume_id,
                        info_hash=t.info_hash,
                        match_method=method,
                        match_tier=tier,
                        score=score,
                    )
                )
            count += 1
        await session.commit()
        return count

    if db is not None:
        return await _do(db)
    async with async_session() as session:
        return await _do(session)


async def run_match_batch(batch_size: int = 50) -> int:
    """Match recently updated torrents against candidate catalog volumes."""
    volumes = await _candidate_volumes()
    if not volumes:
        return 0

    total = 0
    async with async_session() as db:
        torrents = (
            await db.execute(
                select(IndexerTorrent)
                .where(IndexerTorrent.is_active.is_(True))
                .where(IndexerTorrent.media_type.in_(("audiobook", "ebook")))
                .order_by(IndexerTorrent.last_seen_at.desc())
                .limit(batch_size)
            )
        ).scalars().all()

        for t in torrents:
            total += await match_torrent_to_volumes(t, volumes, db)
        await db.commit()
    logger.info("catalog_match batch: %s torrents, %s match rows touched", len(torrents), total)
    return total
