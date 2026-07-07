"""Persistent indexer torrent cache (DMM-style)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, delete, or_, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import IndexerTorrent, CatalogTorrentMatch
from app.services import debrid, real_debrid
from app.services.download_discovery import (
    BookSearchContext,
    build_search_result_payload,
    filter_irrelevant_results,
    order_results_for_display,
    rank_indexer_results,
)

logger = logging.getLogger(__name__)

_ISBN_RE = re.compile(
    r"\b(?:ISBN[- ]*)?(?:97[89][- ]?)?\d{1,5}[- ]?\d{1,7}[- ]?\d{1,7}[- ]?[\dX]\b",
    re.IGNORECASE,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_author_from_title(title: str) -> str:
    if " - " in title:
        return _normalize_text(title.split(" - ", 1)[0])
    by_match = re.search(r"\bby\s+(.+?)(?:\s*[\(\[]|$)", title, re.IGNORECASE)
    if by_match:
        return _normalize_text(by_match.group(1))
    return ""


def extract_isbn(text: str) -> str | None:
    m = _ISBN_RE.search(text or "")
    if not m:
        return None
    digits = re.sub(r"[^0-9X]", "", m.group(0).upper())
    return digits if len(digits) in (10, 13) else None


def _info_hash_from_result(result: dict) -> str | None:
    h = real_debrid.extract_info_hash(
        result.get("magnetUrl"),
        result.get("infoHash") or None,
        result.get("downloadUrl"),
    )
    return h.lower() if h else None


def torrent_row_to_api(row: IndexerTorrent) -> dict[str, Any]:
    return {
        "title": row.title,
        "indexer": row.indexer,
        "size": row.size_bytes,
        "seeders": row.seeders,
        "mediaType": row.media_type,
        "magnetUrl": row.magnet_url,
        "downloadUrl": row.download_url,
        "infoHash": row.info_hash,
        "guid": row.guid,
        "rdCached": row.rd_cached,
        "torboxCached": row.torbox_cached,
        "cachedProviders": [
            p for p, hit in (("rd", row.rd_cached), ("torbox", row.torbox_cached)) if hit
        ],
        "source": "cache",
    }


# SQLite caps bound parameters (999 on older builds) — chunk IN() clauses.
_IN_CLAUSE_CHUNK = 500


def _chunked(items: list, size: int = _IN_CLAUSE_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def upsert_torrents(results: list[dict], db: AsyncSession | None = None) -> int:
    """Insert or update torrent rows from Prowlarr payloads. Returns count upserted.

    Existing rows are fetched in one batched query (instead of one SELECT per
    result) — this matters on a Pi where a scrape job can carry 400+ results.
    """
    if not results:
        return 0

    now = _utcnow()

    # Parse + dedupe within the batch first (last occurrence wins).
    parsed: dict[str, dict] = {}
    for r in results:
        info_hash = _info_hash_from_result(r)
        if not info_hash:
            continue
        title = (r.get("title") or "").strip() or "Unknown"
        parsed[info_hash] = {
            "title": title[:512],
            "indexer": (r.get("indexer") or "")[:128],
            "size_bytes": r.get("size"),
            "seeders": int(r.get("seeders") or 0),
            "media_type": (r.get("mediaType") or "unknown")[:16],
            "magnet_url": r.get("magnetUrl"),
            "download_url": r.get("downloadUrl"),
            "guid": r.get("guid"),
            "parsed_isbn": extract_isbn(title),
            "title_norm": _normalize_text(title)[:512],
            "author_norm": _parse_author_from_title(title)[:256],
            "last_seen_at": now,
            "last_indexer_fetch_at": now,
            "is_active": True,
        }
    if not parsed:
        return 0

    async def _do(session: AsyncSession) -> int:
        hashes = list(parsed.keys())
        existing_map: dict[str, IndexerTorrent] = {}
        for chunk in _chunked(hashes):
            rows = (
                await session.execute(
                    select(IndexerTorrent).where(IndexerTorrent.info_hash.in_(chunk))
                )
            ).scalars().all()
            for row in rows:
                existing_map[row.info_hash] = row

        for info_hash, fields in parsed.items():
            existing = existing_map.get(info_hash)
            if existing:
                for k, v in fields.items():
                    setattr(existing, k, v)
            else:
                session.add(IndexerTorrent(info_hash=info_hash, first_seen_at=now, **fields))
        await session.commit()
        return len(parsed)

    if db is not None:
        return await _do(db)
    async with async_session() as session:
        return await _do(session)


def _fts_phrase(normalized: str) -> str:
    """FTS5 phrase query for already-normalized text (alphanumeric + spaces)."""
    return f'"{normalized}"'


async def _candidate_rows_fts(
    db: AsyncSession, base_norm: str, author_norm: str, limit: int
) -> list[IndexerTorrent]:
    """Candidate lookup via the FTS5 index (fast at any table size)."""
    clauses = [f"title_norm : {_fts_phrase(base_norm)}"]
    if author_norm:
        clauses.append(f"author_norm : {_fts_phrase(author_norm)}")
        # Author name often appears in the title itself ("Sanderson - Mistborn").
        clauses.append(f"title_norm : {_fts_phrase(author_norm)}")
    match_query = " OR ".join(clauses)

    id_rows = await db.execute(
        text(
            "SELECT rowid FROM indexer_torrents_fts "
            "WHERE indexer_torrents_fts MATCH :match_q LIMIT :lim"
        ),
        {"match_q": match_query, "lim": limit},
    )
    ids = [r[0] for r in id_rows]
    if not ids:
        return []

    rows: list[IndexerTorrent] = []
    for chunk in _chunked(ids):
        rows.extend(
            (
                await db.execute(
                    select(IndexerTorrent).where(
                        IndexerTorrent.id.in_(chunk),
                        IndexerTorrent.is_active.is_(True),
                    )
                )
            ).scalars().all()
        )
    return rows


async def _candidate_rows_like(
    db: AsyncSession, base_norm: str, author_norm: str, limit: int
) -> list[IndexerTorrent]:
    """Legacy full-scan fallback (used only if the FTS table is unavailable)."""
    q = select(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
    q = q.where(
        or_(
            IndexerTorrent.title_norm.contains(base_norm[:40]),
            IndexerTorrent.author_norm.contains(author_norm[:30]) if author_norm else False,
        )
    )
    return list((await db.execute(q.limit(limit))).scalars().all())


async def get_torrents_for_book(
    ctx: BookSearchContext,
    tiers: tuple[str, ...] = ("exact", "likely", "weak"),
    max_results: int = 200,
) -> list[dict]:
    """Return cached torrents ranked for a book context."""
    async with async_session() as db:
        base_norm = _normalize_text(ctx.base_title)
        author_norm = _normalize_text(ctx.author)
        if not base_norm:
            rows = (
                await db.execute(
                    select(IndexerTorrent).where(IndexerTorrent.is_active.is_(True)).limit(500)
                )
            ).scalars().all()
        else:
            try:
                rows = await _candidate_rows_fts(db, base_norm, author_norm, 500)
            except Exception as e:
                logger.warning("FTS torrent lookup failed, falling back to LIKE scan: %s", e)
                rows = await _candidate_rows_like(db, base_norm, author_norm, 500)

    raw = [torrent_row_to_api(r) for r in rows]
    relevant, _ = filter_irrelevant_results(raw, ctx)
    ranked = rank_indexer_results(relevant, ctx)
    if tiers != ("exact", "likely", "weak"):
        ranked = [r for r in ranked if r.get("matchTier") in tiers]
    return order_results_for_display(ranked, ctx)[:max_results]


async def volume_ids_with_matches(
    volume_ids: list[str],
    tiers: tuple[str, ...] = ("exact", "likely"),
) -> dict[str, dict]:
    """Batch lookup availability for store grid cards."""
    if not volume_ids:
        return {}
    async with async_session() as db:
        stmt = (
            select(
                CatalogTorrentMatch.google_volume_id,
                func.count(CatalogTorrentMatch.id).label("cnt"),
                func.max(IndexerTorrent.rd_cached).label("any_rd"),
                func.max(IndexerTorrent.torbox_cached).label("any_tb"),
            )
            .join(IndexerTorrent, CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash)
            .where(
                CatalogTorrentMatch.google_volume_id.in_(volume_ids),
                CatalogTorrentMatch.match_tier.in_(tiers),
                IndexerTorrent.is_active.is_(True),
            )
            .group_by(CatalogTorrentMatch.google_volume_id)
        )
        rows = (await db.execute(stmt)).all()

    out: dict[str, dict] = {}
    for vid, cnt, any_rd, any_tb in rows:
        out[vid] = {
            "available": True,
            "matchCount": int(cnt or 0),
            "instantRd": bool(any_rd),
            "instantTorbox": bool(any_tb),
        }
    return out


async def get_volume_availability(volume_id: str) -> dict:
    info = await volume_ids_with_matches([volume_id])
    return info.get(volume_id, {"available": False, "matchCount": 0, "instantRd": False, "instantTorbox": False})


async def enrich_debrid_flags(hashes: list[str], db: AsyncSession | None = None) -> int:
    """Batch-check debrid cache and update indexer_torrents rows."""
    if not hashes or not debrid.available_providers():
        return 0

    cached = await debrid.check_cached_all(hashes)
    now = _utcnow()

    async def _do(session: AsyncSession) -> int:
        updated = 0
        lower = [h.lower() for h in hashes]
        by_lower = {h.lower(): h for h in hashes}
        for chunk in _chunked(lower):
            rows = (
                await session.execute(
                    select(IndexerTorrent).where(IndexerTorrent.info_hash.in_(chunk))
                )
            ).scalars().all()
            for row in rows:
                h = by_lower.get(row.info_hash, row.info_hash)
                row.rd_cached = h in cached.get(debrid.RD, set())
                row.torbox_cached = h in cached.get(debrid.TORBOX, set())
                row.last_debrid_check_at = now
                updated += 1
        await session.commit()
        return updated

    if db is not None:
        return await _do(db)
    async with async_session() as session:
        return await _do(session)


async def hashes_needing_debrid_check(limit: int = 100) -> list[str]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(IndexerTorrent.info_hash)
                .where(IndexerTorrent.is_active.is_(True))
                .order_by(IndexerTorrent.last_debrid_check_at.asc().nullsfirst())
                .limit(limit)
            )
        ).scalars().all()
    return list(rows)


async def torrent_count() -> int:
    async with async_session() as db:
        return (
            await db.execute(
                select(func.count()).select_from(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
            )
        ).scalar_one()


async def prune_stale(days: int) -> int:
    from datetime import timedelta

    cutoff = _utcnow() - timedelta(days=days)
    async with async_session() as db:
        result = await db.execute(
            update(IndexerTorrent)
            .where(IndexerTorrent.last_seen_at < cutoff, IndexerTorrent.is_active.is_(True))
            .values(is_active=False)
        )
        await db.commit()
        return result.rowcount or 0


async def prune_non_book_torrents() -> int:
    """Deactivate cached rows that are not actually books (e.g. Knaben video/software noise).

    Loads only the columns the filter needs (not full ORM rows) and flips
    is_active with set-based UPDATEs — this sweep walks the whole table.
    """
    from app.services.prowlarr import is_book_related

    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    IndexerTorrent.id,
                    IndexerTorrent.title,
                    IndexerTorrent.indexer,
                    IndexerTorrent.media_type,
                ).where(IndexerTorrent.is_active.is_(True))
            )
        ).all()

        bad_ids = [
            row_id
            for row_id, title, indexer, media_type in rows
            if media_type not in ("audiobook", "ebook")
            or not is_book_related([], title=title, indexer=indexer)
        ]
        for chunk in _chunked(bad_ids):
            await db.execute(
                update(IndexerTorrent).where(IndexerTorrent.id.in_(chunk)).values(is_active=False)
            )
        await db.commit()
        return len(bad_ids)


async def refresh_live_and_merge(
    ctx: BookSearchContext,
    live_results: list[dict],
    max_return: int,
) -> dict:
    """Upsert live Prowlarr results and return ranked payload."""
    await upsert_torrents(live_results)
    payload = build_search_result_payload(live_results, ctx, max_return)
    return payload
