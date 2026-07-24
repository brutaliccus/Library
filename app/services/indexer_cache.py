"""Persistent indexer torrent cache (DMM-style)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models import IndexerTorrent, CatalogTorrentMatch
from app.services import debrid, real_debrid
from app.services.download_discovery import (
    BookSearchContext,
    build_search_result_payload,
    filter_irrelevant_results,
    order_results_for_display,
    rank_indexer_results,
    resolve_book_search_context,
)

logger = logging.getLogger(__name__)

# Ebook packs/comic archives on Knaben are often 10–100+ GiB; cap ingest at 1 GiB.
MAX_EBOOK_SIZE_BYTES = 1_073_741_824


def ebook_size_acceptable(media_type: str, size_bytes: int | None) -> bool:
    if (media_type or "").lower() != "ebook":
        return True
    if not size_bytes or size_bytes <= 0:
        return True
    cap = int(getattr(get_settings(), "max_ebook_bytes", MAX_EBOOK_SIZE_BYTES) or MAX_EBOOK_SIZE_BYTES)
    return size_bytes <= cap

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


def _is_audiobookbay_indexer_name(name: str) -> bool:
    compact = (name or "").lower().replace(" ", "")
    if "audiobookbay" in compact:
        return True
    return "audiobook" in compact and "bay" in compact


async def upsert_torrents(results: list[dict], db: AsyncSession | None = None) -> int:
    """Insert or update torrent rows from Prowlarr payloads. Returns count upserted.

    Existing rows are fetched in one batched query (instead of one SELECT per
    result) — this matters on a Pi where a scrape job can carry 400+ results.
    """
    if not results:
        return 0

    from app.services.prowlarr import title_is_mostly_foreign_script
    from app.services import scraper_settings
    from app.services.rss_content_filters import (
        is_too_small_for_audiobook,
        title_is_non_book,
    )

    now = _utcnow()
    try:
        cfg = await scraper_settings.get_scraper_config()
        prune_foreign = bool(cfg.foreign_title_prune)
    except Exception:
        prune_foreign = True

    # Parse + dedupe within the batch first (last occurrence wins).
    parsed: dict[str, dict] = {}
    for r in results:
        info_hash = _info_hash_from_result(r)
        if not info_hash:
            continue
        title = (r.get("title") or "").strip() or "Unknown"
        # Never store titles that are majority non-Latin script (CJK / Cyrillic /
        # Hangul / etc.) — same rule used by prune_non_book_torrents.
        if prune_foreign and title_is_mostly_foreign_script(title):
            continue
        # Adult / music / movie / software — reject before DB + debrid path.
        if title_is_non_book(title):
            continue
        media_type = (r.get("mediaType") or "unknown")[:16]
        size_bytes = r.get("size")
        if is_too_small_for_audiobook(size_bytes, media_type):
            continue
        if not ebook_size_acceptable(media_type, size_bytes):
            continue
        parsed[info_hash] = {
            "title": title[:512],
            "indexer": (r.get("indexer") or "")[:128],
            "size_bytes": size_bytes,
            "seeders": int(r.get("seeders") or 0),
            "media_type": media_type,
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
                new_indexer = fields.get("indexer") or ""
                if _is_audiobookbay_indexer_name(existing.indexer):
                    fields["indexer"] = existing.indexer
                elif _is_audiobookbay_indexer_name(new_indexer):
                    fields["indexer"] = new_indexer
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
    """Candidate lookup via the FTS5 index (fast at any table size).

    Title-first: match the book title tokens. Author is never used alone — that
    pulled entire author catalogs (e.g. all Matt Dinniman) into Find Downloads.
    """
    tokens = [t for t in (base_norm or "").split() if len(t) > 1]
    if not tokens:
        return []

    # All significant title tokens must appear somewhere in title_norm.
    # FTS5 default tokenizer is space-separated; AND beats a long phrase when
    # release names insert dashes/noise between words.
    title_match = " AND ".join(tokens)
    if author_norm:
        # Prefer rows that also mention the author, but OR a title-only match
        # so "Title without author in name" still returns.
        match_query = f"(({title_match}) AND ({_fts_phrase(author_norm)})) OR ({title_match})"
    else:
        match_query = title_match

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
    tokens = [t for t in (base_norm or "").split() if len(t) > 2][:6]
    if not tokens:
        needle = (base_norm or "")[:40]
        if not needle:
            return []
        q = select(IndexerTorrent).where(
            IndexerTorrent.is_active.is_(True),
            IndexerTorrent.title_norm.contains(needle),
        )
        return list((await db.execute(q.limit(limit))).scalars().all())

    q = select(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
    for tok in tokens:
        q = q.where(IndexerTorrent.title_norm.contains(tok))
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


async def search_cache_releases(
    query: str,
    *,
    limit: int = 24,
    unmatched_only: bool = True,
) -> list[dict]:
    """Title-search the indexer cache for store search cards.

    Returns card-shaped dicts with optional cover art. When ``unmatched_only``
    is set, skips torrents that already have a catalog match (those surface as
    normal book cards).
    """
    from app.services.catalog_match import _clean_release_text, _torrent_search_queries

    q = (query or "").strip()
    if len(q) < 2:
        return []

    book_ctx = resolve_book_search_context(title=q, author="")
    base_norm = _normalize_text(book_ctx.base_title or q)
    if not base_norm:
        return []

    async with async_session() as db:
        try:
            rows = await _candidate_rows_fts(db, base_norm, "", min(200, limit * 8))
        except Exception as e:
            logger.warning("FTS cache release search failed, LIKE fallback: %s", e)
            rows = await _candidate_rows_like(db, base_norm, "", min(200, limit * 8))

        rows = [
            r for r in rows
            if r.media_type in ("audiobook", "ebook") and r.is_active
        ]

        matched_hashes: set[str] = set()
        if unmatched_only and rows:
            hashes = [r.info_hash for r in rows]
            for chunk in _chunked(hashes):
                existing = (
                    await db.execute(
                        select(CatalogTorrentMatch.info_hash)
                        .where(CatalogTorrentMatch.info_hash.in_(chunk))
                        .distinct()
                    )
                ).scalars().all()
                matched_hashes.update(existing)

            # Prefer unmatched, but if none, still show top ranked matches so the
            # section isn't empty when everything is already linked.
            unmatched = [r for r in rows if r.info_hash not in matched_hashes]
            rows = unmatched if unmatched else rows

        # Pull cover from an existing catalog match when present.
        cover_by_hash: dict[str, str] = {}
        volume_by_hash: dict[str, str] = {}
        if rows:
            hashes = [r.info_hash for r in rows]
            for chunk in _chunked(hashes):
                links = (
                    await db.execute(
                        select(
                            CatalogTorrentMatch.info_hash,
                            CatalogTorrentMatch.google_volume_id,
                        )
                        .where(
                            CatalogTorrentMatch.info_hash.in_(chunk),
                            CatalogTorrentMatch.match_tier.in_(("exact", "likely")),
                        )
                        .order_by(CatalogTorrentMatch.score.desc())
                    )
                ).all()
                for info_hash, vid in links:
                    if info_hash not in volume_by_hash:
                        volume_by_hash[info_hash] = vid

    # Rank by title relevance to the user query.
    raw = [torrent_row_to_api(r) for r in rows]
    ranked = rank_indexer_results(raw, book_ctx)
    ranked = order_results_for_display(ranked, book_ctx)[:limit]

    # Resolve covers: matched volume → OL/ISBNdb title guess → empty.
    from app.services import google_books, isbndb, ol_catalog

    async def _cover_for(title: str, info_hash: str) -> str:
        vid = volume_by_hash.get(info_hash)
        if vid:
            try:
                book = await google_books.get_catalog_volume(vid)
                if book and book.get("coverUrl"):
                    return book["coverUrl"]
            except Exception:
                pass
        guesses = _torrent_search_queries(title) or [_clean_release_text(title)]
        for guess in guesses[:1]:
            if not guess:
                continue
            try:
                if ol_catalog.catalog_ready():
                    hits = await ol_catalog.search_by_title(guess, limit=1)
                    if hits and hits[0].get("coverUrl"):
                        return hits[0]["coverUrl"]
            except Exception:
                pass
            try:
                result = await isbndb.search_books(guess, limit=1)
                books = result.get("books") or []
                if books and books[0].get("coverUrl"):
                    return books[0]["coverUrl"]
            except Exception:
                pass
        return ""

    cards: list[dict] = []
    for item in ranked:
        info_hash = item.get("infoHash") or ""
        title = item.get("title") or ""
        cover = await _cover_for(title, info_hash) if info_hash else ""
        display_title = _clean_release_text(title) or title
        author = ""
        if " - " in display_title:
            parts = [p.strip() for p in display_title.split(" - ") if p.strip()]
            if len(parts) >= 2:
                # Heuristic: shorter segment often author on ABB "Title - Author"
                if len(parts[0]) >= len(parts[-1]):
                    display_title, author = parts[0], parts[-1]
                else:
                    author, display_title = parts[0], parts[-1]
        cards.append(
            {
                "id": f"cache:{info_hash}",
                "volumeId": f"cache:{info_hash}",
                "title": display_title[:200],
                "subtitle": "",
                "authors": [author] if author else [],
                "publisher": "",
                "publishedDate": "",
                "description": title,
                "pageCount": 0,
                "categories": [],
                "mainCategory": "",
                "averageRating": 0,
                "ratingsCount": 0,
                "language": "",
                "coverUrl": cover,
                "isbn10": "",
                "isbn13": "",
                "previewLink": "",
                "infoLink": "",
                "releaseTitle": title,
                "infoHash": info_hash,
                "magnetUrl": item.get("magnetUrl"),
                "downloadUrl": item.get("downloadUrl"),
                "mediaType": item.get("mediaType"),
                "indexer": item.get("indexer"),
                "size": item.get("size"),
                "seeders": item.get("seeders"),
                "rdCached": item.get("rdCached"),
                "torboxCached": item.get("torboxCached"),
                "catalogMatched": info_hash in volume_by_hash,
                "availability": {
                    "available": True,
                    "matchCount": 1,
                    "instantRd": bool(item.get("rdCached")),
                    "instantTorbox": bool(item.get("torboxCached")),
                },
                "source": "cache_release",
            }
        )
    return cards


async def get_cache_release_detail(info_hash: str) -> dict | None:
    """Build a BookDetail-shaped payload for a cache-only torrent card."""
    from app.services.catalog_match import _clean_release_text, _torrent_search_queries
    from app.services import isbndb, ol_catalog

    h = (info_hash or "").strip().lower()
    if not h:
        return None

    async with async_session() as db:
        row = (
            await db.execute(
                select(IndexerTorrent).where(IndexerTorrent.info_hash == h)
            )
        ).scalar_one_or_none()
        if row is None:
            # Try case-insensitive / original casing
            row = (
                await db.execute(
                    select(IndexerTorrent).where(IndexerTorrent.info_hash == info_hash)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        api = torrent_row_to_api(row)
        title_raw = row.title or ""
        info_hash_out = row.info_hash

    display = _clean_release_text(title_raw) or title_raw
    author = ""
    if " - " in display:
        parts = [p.strip() for p in display.split(" - ") if p.strip()]
        if len(parts) >= 2:
            if len(parts[0]) >= len(parts[-1]):
                display, author = parts[0], parts[-1]
            else:
                author, display = parts[0], parts[-1]

    cover = ""
    for guess in (_torrent_search_queries(title_raw) or [display])[:1]:
        if not guess:
            continue
        try:
            if ol_catalog.catalog_ready():
                hits = await ol_catalog.search_by_title(guess, limit=1)
                if hits and hits[0].get("coverUrl"):
                    cover = hits[0]["coverUrl"]
                    break
        except Exception:
            pass
        try:
            result = await isbndb.search_books(guess, limit=1)
            books = result.get("books") or []
            if books and books[0].get("coverUrl"):
                cover = books[0]["coverUrl"]
                break
        except Exception:
            pass

    return {
        "id": f"cache:{info_hash_out}",
        "volumeId": f"cache:{info_hash_out}",
        "title": display[:200],
        "subtitle": "",
        "authors": [author] if author else [],
        "publisher": "",
        "publishedDate": "",
        "description": (
            f"Cached indexer release without an Open Library catalog match.\n\n"
            f"Release: {title_raw}"
        ),
        "pageCount": 0,
        "categories": [],
        "mainCategory": "",
        "averageRating": 0,
        "ratingsCount": 0,
        "language": "",
        "coverUrl": cover,
        "coverUrlLarge": cover,
        "isbn10": "",
        "isbn13": "",
        "previewLink": "",
        "infoLink": "",
        "printType": "BOOK",
        "releaseTitle": title_raw,
        "infoHash": info_hash_out,
        "magnetUrl": api.get("magnetUrl"),
        "downloadUrl": api.get("downloadUrl"),
        "mediaType": api.get("mediaType"),
        "rdCached": api.get("rdCached"),
        "torboxCached": api.get("torboxCached"),
        "source": "cache_release",
        "availability": {
            "available": True,
            "matchCount": 1,
            "instantRd": bool(api.get("rdCached")),
            "instantTorbox": bool(api.get("torboxCached")),
        },
    }


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


_MATCH_TIERS = ("exact", "likely")


# --- Matched-volume summary table -------------------------------------------
# Aggregating the 90k+ match⋈torrent join with GROUP BY + COUNT(DISTINCT) on
# every store-tab load costs ~4s on the Pi. Instead we materialize a compact,
# indexed summary table (one row per matched volume) that the background scraper
# refreshes periodically; the store then reads it with millisecond queries.
_MATCHED_TIERS_SQL = "('exact','likely')"
_MATCHED_REFRESH_MIN_INTERVAL = 300.0  # seconds between rebuilds (throttle)
_summary_lock = asyncio.Lock()
_summary_ready = False
_summary_last_refresh = 0.0
_summary_schema_ok = False


async def _ensure_summary_schema(db: AsyncSession) -> None:
    global _summary_schema_ok
    if _summary_schema_ok:
        return
    await db.execute(text(
        "CREATE TABLE IF NOT EXISTS matched_volumes ("
        " google_volume_id TEXT PRIMARY KEY,"
        " best_score REAL NOT NULL DEFAULT 0,"
        " latest_updated TEXT )"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_matched_volumes_score "
        "ON matched_volumes (best_score DESC)"
    ))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_matched_volumes_updated "
        "ON matched_volumes (latest_updated DESC)"
    ))
    # Persistent per-volume subject cache (populated lazily from the local OL
    # catalog) so genre browse can start from "volumes we have torrents for"
    # and filter by subject — instead of scanning the giant OL subject index
    # and finding only the tiny overlap. Kept across summary rebuilds; an FTS5
    # mirror gives fast subject MATCH over the (tens-of-thousands) matched set.
    await db.execute(text(
        "CREATE TABLE IF NOT EXISTS volume_subjects ("
        " google_volume_id TEXT PRIMARY KEY,"
        " subjects TEXT,"
        " year INTEGER )"
    ))
    # Add `year` to pre-existing installs (CREATE IF NOT EXISTS won't alter it).
    try:
        cols = (await db.execute(text("PRAGMA table_info(volume_subjects)"))).all()
        if not any((c[1] == "year") for c in cols):
            await db.execute(text("ALTER TABLE volume_subjects ADD COLUMN year INTEGER"))
    except Exception as e:  # pragma: no cover
        logger.debug("volume_subjects year column check failed: %s", e)
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_volume_subjects_year "
        "ON volume_subjects (year DESC)"
    ))
    await db.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS volume_subjects_fts "
        "USING fts5(google_volume_id UNINDEXED, subjects, tokenize='unicode61')"
    ))
    _summary_schema_ok = True


async def mark_summary_ready_if_populated() -> bool:
    """If the persisted matched_volumes table already has rows, flag the summary
    ready so reads serve from it (fast) instead of the live aggregate. Returns
    True when populated. Lets a restart reuse the last snapshot and defer the
    heavy rebuild off the cold-start critical path.
    """
    global _summary_ready
    try:
        async with async_session() as db:
            await _ensure_summary_schema(db)
            n = await db.scalar(text("SELECT COUNT(*) FROM matched_volumes")) or 0
    except Exception as e:  # pragma: no cover
        logger.warning("summary populated check failed: %s", e)
        return False
    if int(n) > 0:
        _summary_ready = True
        logger.info("matched_volumes summary already populated (%s rows) — serving warm", n)
        return True
    return False


async def refresh_matched_volumes(force: bool = False) -> int:
    """Rebuild the matched_volumes summary table from live matches.

    The heavy aggregate is computed into a connection-local TEMP table (reads
    only — no main-DB write lock while it scans the 90k-row join), then swapped
    into place under a short write lock. Throttled to avoid rebuilding more than
    once per ``_MATCHED_REFRESH_MIN_INTERVAL`` unless ``force`` is set. Atomic to
    readers: they see the previous snapshot until commit. Returns volume count.
    """
    global _summary_ready, _summary_last_refresh
    async with _summary_lock:
        now = time.monotonic()
        if (
            not force
            and _summary_ready
            and (now - _summary_last_refresh) < _MATCHED_REFRESH_MIN_INTERVAL
        ):
            return -1  # skipped (still fresh)
        async with async_session() as db:
            await _ensure_summary_schema(db)
            # Heavy aggregate -> temp table: scans main DB read-only, so other
            # writers aren't blocked during the ~20s build.
            await db.execute(text("DROP TABLE IF EXISTS temp.tmp_matched_volumes"))
            await db.execute(text(
                "CREATE TEMP TABLE tmp_matched_volumes AS "
                "SELECT m.google_volume_id AS google_volume_id, "
                " MAX(m.score) AS best_score, MAX(m.updated_at) AS latest_updated "
                "FROM catalog_torrent_matches m "
                "JOIN indexer_torrents t ON m.info_hash = t.info_hash "
                f"WHERE m.match_tier IN {_MATCHED_TIERS_SQL} AND t.is_active = 1 "
                "GROUP BY m.google_volume_id"
            ))
            # Fast swap under a short write lock.
            await db.execute(text("DELETE FROM matched_volumes"))
            await db.execute(text(
                "INSERT INTO matched_volumes (google_volume_id, best_score, latest_updated) "
                "SELECT google_volume_id, best_score, latest_updated FROM tmp_matched_volumes"
            ))
            await db.execute(text("DROP TABLE IF EXISTS temp.tmp_matched_volumes"))
            await db.commit()
            n = await db.scalar(text("SELECT COUNT(*) FROM matched_volumes")) or 0
        _summary_ready = True
        _summary_last_refresh = time.monotonic()
        logger.info("matched_volumes summary refreshed: %s volumes", n)
    # Subject-tag any newly matched volumes (incremental; runs outside the lock
    # above so a long OL lookup can't stall interactive store reads).
    try:
        await refresh_volume_subjects()
    except Exception as e:  # pragma: no cover - best-effort enrichment
        logger.warning("volume_subjects refresh failed: %s", e)
    return int(n)


_subjects_lock = asyncio.Lock()


async def _scrub_insane_publish_years(db) -> int:
    """Zero out OL dump garbage years (9999, 9881, …) so they cannot float to the top."""
    from datetime import datetime, timezone

    from app.services.ol_catalog import _MIN_PUBLISH_YEAR

    max_year = datetime.now(timezone.utc).year + 1
    try:
        result = await db.execute(
            text(
                "UPDATE volume_subjects SET year = 0 "
                "WHERE year IS NOT NULL AND (year < :miny OR year > :maxy)"
            ),
            {"miny": _MIN_PUBLISH_YEAR, "maxy": max_year},
        )
        return int(result.rowcount or 0)
    except Exception as e:  # pragma: no cover
        logger.debug("volume_subjects year scrub failed: %s", e)
        return 0


async def refresh_volume_subjects(limit: int | None = None) -> int:
    """Populate subjects + publish year for matched volumes.

    Two incremental passes, both cheap in steady state:
      * NEW: matched volumes with no volume_subjects row -> insert subjects (+FTS)
        and real publish year from the local OL catalog.
      * BACKFILL: existing rows with year IS NULL (tagged before year was stored)
        -> update just the year, leaving subjects/FTS untouched.
    Looked-up misses are stored (empty subjects / year 0) so they aren't retried.
    Returns the number of volumes touched.
    """
    from app.services import ol_catalog

    # Hygiene does not need the OL catalog — dump garbage years poison shelves.
    scrubbed = 0
    try:
        async with async_session() as db:
            await _ensure_summary_schema(db)
            scrubbed = await _scrub_insane_publish_years(db)
            if scrubbed:
                await db.commit()
                logger.info("volume_subjects: scrubbed %s insane publish years", scrubbed)
    except Exception as e:
        logger.warning("volume_subjects year scrub failed: %s", e)

    if not ol_catalog.catalog_ready():
        return scrubbed

    async with _subjects_lock:
        cap = int(limit) if limit else 5000
        async with async_session() as db:
            await _ensure_summary_schema(db)
            new_rows = (await db.execute(
                text(
                    "SELECT m.google_volume_id FROM matched_volumes m "
                    "LEFT JOIN volume_subjects v ON v.google_volume_id = m.google_volume_id "
                    "WHERE v.google_volume_id IS NULL LIMIT :lim"
                ),
                {"lim": cap},
            )).all()
            new_ids = [r[0] for r in new_rows if r[0]]
            year_rows = (await db.execute(
                text(
                    "SELECT google_volume_id FROM volume_subjects "
                    "WHERE year IS NULL LIMIT :lim"
                ),
                {"lim": cap},
            )).all()
            year_ids = [r[0] for r in year_rows if r[0]]

        if not new_ids and not year_ids:
            return scrubbed

        # OL lookups on a private read-only connection (never blocks app writes).
        all_ids = list({*new_ids, *year_ids})
        conn = await ol_catalog.open_private_connection()
        meta: dict[str, tuple[str, int]] = {}
        try:
            for chunk in _chunked(all_ids, size=400):
                got = await ol_catalog.subjects_for_works(chunk, conn=conn)
                for vid in chunk:
                    meta[vid] = got.get(vid, ("", 0))
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass

        new_set = set(new_ids)
        touched = 0
        # Commit in small batches so we never hold a long write lock on the Pi.
        for batch in _chunked(all_ids, size=1000):
            async with async_session() as db:
                await _ensure_summary_schema(db)
                for vid in batch:
                    subj, year = meta.get(vid, ("", 0))
                    year = ol_catalog.sane_publish_year(year)
                    if vid in new_set:
                        await db.execute(
                            text(
                                "INSERT OR REPLACE INTO volume_subjects "
                                "(google_volume_id, subjects, year) VALUES (:v, :s, :y)"
                            ),
                            {"v": vid, "s": subj, "y": int(year)},
                        )
                        if subj:
                            await db.execute(
                                text(
                                    "INSERT INTO volume_subjects_fts "
                                    "(google_volume_id, subjects) VALUES (:v, :s)"
                                ),
                                {"v": vid, "s": subj},
                            )
                    else:
                        # Year backfill only — subjects/FTS already populated.
                        await db.execute(
                            text(
                                "UPDATE volume_subjects SET year = :y "
                                "WHERE google_volume_id = :v"
                            ),
                            {"v": vid, "y": int(year)},
                        )
                    touched += 1
                await db.commit()
        logger.info(
            "volume_subjects: %s new + %s year-backfill volumes tagged",
            len(new_ids), len(year_ids),
        )
        return touched + scrubbed


async def list_matched_volumes_by_year(
    *, page: int = 1, page_size: int = 20, min_year: int = 0,
) -> tuple[list[str], int]:
    """Matched volume ids ordered by REAL publication year (newest first).

    Secondary sort is ``latest_updated`` so ties within a year rotate as the
    scraper matches new torrents. Insane OL dump years are excluded.
    """
    from datetime import datetime, timezone

    from app.services.ol_catalog import _MIN_PUBLISH_YEAR

    start = (page - 1) * page_size
    max_year = datetime.now(timezone.utc).year + 1
    floor = max(int(min_year or 0), _MIN_PUBLISH_YEAR)
    async with async_session() as db:
        await _ensure_summary_schema(db)
        try:
            rows = (await db.execute(
                text(
                    "SELECT v.google_volume_id FROM volume_subjects v "
                    "JOIN matched_volumes m ON m.google_volume_id = v.google_volume_id "
                    "WHERE v.year IS NOT NULL AND v.year >= :minyear AND v.year <= :maxyear "
                    "ORDER BY v.year DESC, m.latest_updated DESC, m.best_score DESC "
                    "LIMIT :lim OFFSET :off"
                ),
                {
                    "minyear": floor,
                    "maxyear": max_year,
                    "lim": page_size,
                    "off": start,
                },
            )).all()
        except Exception as e:
            logger.warning("by-year matched query failed: %s", e)
            return [], 0
        return [r[0] for r in rows if r[0]], 0


async def list_matched_volume_ids_by_subject(
    match_expr: str,
    *,
    page: int = 1,
    page_size: int = 20,
    order_by: str = "score",
    need_total: bool = False,
) -> tuple[list[str], int]:
    """Matched volume ids whose subjects match an FTS expression, paged.

    This is the genre-browse fast path: it starts from the volumes we actually
    have active torrents for (matched_volumes) and filters them by subject via
    the volume_subjects_fts index, ordered by match score. Returns hundreds of
    hits for broad genres instead of the ~5 the old OL-subject-first scan found.
    """
    if not match_expr:
        return [], 0
    start = (page - 1) * page_size
    order_col = "m.latest_updated" if order_by == "recent" else "m.best_score"

    async with async_session() as db:
        await _ensure_summary_schema(db)
        try:
            rows = (await db.execute(
                text(
                    "SELECT m.google_volume_id "
                    "FROM volume_subjects_fts f "
                    "JOIN matched_volumes m ON m.google_volume_id = f.google_volume_id "
                    "WHERE volume_subjects_fts MATCH :expr "
                    f"ORDER BY {order_col} DESC LIMIT :lim OFFSET :off"
                ),
                {"expr": match_expr, "lim": page_size, "off": start},
            )).all()
        except Exception as e:
            logger.warning("subject-matched volume query failed for %r: %s", match_expr, e)
            return [], 0

        total = 0
        if need_total:
            try:
                total = await db.scalar(
                    text(
                        "SELECT COUNT(*) FROM volume_subjects_fts f "
                        "JOIN matched_volumes m ON m.google_volume_id = f.google_volume_id "
                        "WHERE volume_subjects_fts MATCH :expr"
                    ),
                    {"expr": match_expr},
                ) or 0
            except Exception:
                total = 0
        return [row[0] for row in rows], int(total)


async def _list_matched_live(
    db: AsyncSession, *, start: int, page_size: int, order_by: str, need_total: bool
) -> tuple[list[str], int]:
    """Fallback: aggregate directly off the join (used before first refresh)."""
    from sqlalchemy import desc

    match_filter = (
        CatalogTorrentMatch.match_tier.in_(_MATCH_TIERS),
        IndexerTorrent.is_active.is_(True),
    )
    total = 0
    if need_total:
        total = await db.scalar(
            select(func.count(func.distinct(CatalogTorrentMatch.google_volume_id)))
            .select_from(CatalogTorrentMatch)
            .join(IndexerTorrent, CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash)
            .where(*match_filter)
        ) or 0

    sort_col = (
        func.max(CatalogTorrentMatch.updated_at)
        if order_by == "recent"
        else func.max(CatalogTorrentMatch.score)
    )
    stmt = (
        select(CatalogTorrentMatch.google_volume_id, sort_col.label("sort_key"))
        .join(IndexerTorrent, CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash)
        .where(*match_filter)
        .group_by(CatalogTorrentMatch.google_volume_id)
        .order_by(desc("sort_key"))
        .offset(start)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).all()
    return [row[0] for row in rows], int(total)


async def list_matched_volume_ids(
    *,
    page: int = 1,
    page_size: int = 20,
    order_by: str = "score",
    need_total: bool = True,
) -> tuple[list[str], int]:
    """Paged Google volume ids that have active indexer catalog matches.

    Reads the pre-aggregated ``matched_volumes`` summary table (fast, indexed).
    Falls back to the live aggregate if the summary hasn't been built yet.
    """
    start = (page - 1) * page_size
    order_col = "latest_updated" if order_by == "recent" else "best_score"

    async with async_session() as db:
        try:
            await _ensure_summary_schema(db)
            rows = (await db.execute(
                text(
                    "SELECT google_volume_id FROM matched_volumes "
                    f"ORDER BY {order_col} DESC LIMIT :lim OFFSET :off"
                ),
                {"lim": page_size, "off": start},
            )).all()
            if rows or _summary_ready:
                total = 0
                if need_total:
                    total = await db.scalar(
                        text("SELECT COUNT(*) FROM matched_volumes")
                    ) or 0
                return [row[0] for row in rows], int(total)
        except Exception as e:  # pragma: no cover - summary missing/corrupt
            logger.warning("matched_volumes read failed, using live query: %s", e)

        # Summary not ready yet: serve from the live aggregate this once.
        return await _list_matched_live(
            db, start=start, page_size=page_size,
            order_by=order_by, need_total=need_total,
        )


async def matched_volume_count() -> int:
    """Count of catalog volumes with at least one active indexer match."""
    _, total = await list_matched_volume_ids(page=1, page_size=1)
    return total


async def _magnets_for_rd_probe(
    hashes: list[str],
    limit: int,
    db: AsyncSession,
) -> list[tuple[str, str]]:
    """Torbox-cached rows missing RD — best candidates for blind-add cache probes."""
    if limit <= 0:
        return []

    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    if hashes:
        lower = [h.lower() for h in hashes]
        for chunk in _chunked(lower):
            rows = (
                await db.execute(
                    select(IndexerTorrent.info_hash, IndexerTorrent.magnet_url)
                    .where(
                        IndexerTorrent.info_hash.in_(chunk),
                        IndexerTorrent.torbox_cached.is_(True),
                        IndexerTorrent.rd_cached.is_(False),
                        IndexerTorrent.magnet_url.isnot(None),
                        IndexerTorrent.magnet_url != "",
                    )
                )
            ).all()
            for info_hash, magnet in rows:
                if info_hash and magnet and info_hash not in seen:
                    seen.add(info_hash)
                    items.append((info_hash, magnet))
                    if len(items) >= limit:
                        return items

    remaining = limit - len(items)
    if remaining > 0:
        extra = (
            await db.execute(
                select(IndexerTorrent.info_hash, IndexerTorrent.magnet_url)
                .where(
                    IndexerTorrent.is_active.is_(True),
                    IndexerTorrent.torbox_cached.is_(True),
                    IndexerTorrent.rd_cached.is_(False),
                    IndexerTorrent.magnet_url.isnot(None),
                    IndexerTorrent.magnet_url != "",
                )
                .order_by(IndexerTorrent.last_debrid_check_at.asc().nullsfirst())
                .limit(remaining + len(seen))
            )
        ).all()
        for info_hash, magnet in extra:
            if info_hash and magnet and info_hash not in seen:
                seen.add(info_hash)
                items.append((info_hash, magnet))
                if len(items) >= limit:
                    break

    return items


async def enrich_debrid_flags(
    hashes: list[str],
    db: AsyncSession | None = None,
    *,
    rd_probe_limit: int = 8,
) -> int:
    """Batch-check debrid cache and update indexer_torrents rows."""
    from app.services import real_debrid
    from app.services.debrid_tokens import apply_server_debrid_tokens

    await apply_server_debrid_tokens()
    if not hashes or not debrid.available_providers():
        return 0

    cached = await debrid.check_cached_all(hashes)
    now = _utcnow()

    rd_account_ids: dict[str, str] = {}
    if debrid.RD in debrid.available_providers():
        try:
            rd_account_ids = await real_debrid._account_hash_map()
        except Exception:
            rd_account_ids = {}

    if (
        debrid.RD in debrid.available_providers()
        and real_debrid.instant_availability_disabled()
        and rd_probe_limit > 0
    ):
        async with async_session() as probe_db:
            probe_items = await _magnets_for_rd_probe(hashes, rd_probe_limit, probe_db)
        if probe_items:
            probed = await real_debrid.probe_magnets_cached(
                probe_items,
                max_items=rd_probe_limit,
            )
            if probed:
                cached = dict(cached)
                cached[debrid.RD] = cached.get(debrid.RD, set()) | probed
                try:
                    rd_account_ids.update(
                        await real_debrid._account_hash_map(force_refresh=True)
                    )
                except Exception:
                    pass

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
                rd_hit = h in cached.get(debrid.RD, set()) or bool(row.rd_debrid_id)
                row.rd_cached = rd_hit
                row.torbox_cached = h in cached.get(debrid.TORBOX, set())
                if h in rd_account_ids and not row.rd_debrid_id:
                    row.rd_debrid_id = rd_account_ids[h]
                row.last_debrid_check_at = now
                updated += 1
        await session.commit()
        return updated

    from app.database import run_with_sqlite_retry

    async def _run() -> int:
        if db is not None:
            return await _do(db)
        async with async_session() as session:
            return await _do(session)

    return await run_with_sqlite_retry(_run)


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


async def reset_stale_debrid_flags(limit: int = 500) -> int:
    """Re-queue torrents checked with no instant hits so enrichment can retry."""
    from sqlalchemy import update
    from app.database import run_with_sqlite_retry

    async def _do() -> int:
        async with async_session() as db:
            subq = (
                select(IndexerTorrent.info_hash)
                .where(
                    IndexerTorrent.is_active.is_(True),
                    IndexerTorrent.info_hash != "",
                    IndexerTorrent.rd_cached.is_(False),
                    IndexerTorrent.torbox_cached.is_(False),
                )
                .order_by(IndexerTorrent.last_debrid_check_at.asc().nullsfirst())
                .limit(limit)
            )
            hashes = list((await db.execute(subq)).scalars().all())
            if not hashes:
                return 0
            result = await db.execute(
                update(IndexerTorrent)
                .where(IndexerTorrent.info_hash.in_(hashes))
                .values(last_debrid_check_at=None)
            )
            await db.commit()
            return result.rowcount or 0

    return await run_with_sqlite_retry(_do)


async def queue_all_debrid_recheck(*, clear_preload_ids: bool = True) -> int:
    """Mark every active torrent for a fresh debrid cache check (and optional preload retry)."""
    from sqlalchemy import update
    from app.database import run_with_sqlite_retry

    values: dict = {"last_debrid_check_at": None}
    if clear_preload_ids:
        values.update(
            {
                "rd_debrid_id": None,
                "torbox_debrid_id": None,
                "rd_preloaded_at": None,
                "torbox_preloaded_at": None,
            }
        )

    async def _do() -> int:
        async with async_session() as db:
            result = await db.execute(
                update(IndexerTorrent)
                .where(IndexerTorrent.is_active.is_(True))
                .values(**values)
            )
            await db.commit()
            return result.rowcount or 0

    return await run_with_sqlite_retry(_do, attempts=8, base_delay=1.0)


async def torbox_rd_gap_count() -> int:
    """Torbox-cached torrents not yet flagged on Real-Debrid."""
    async with async_session() as db:
        return (
            await db.scalar(
                select(func.count())
                .select_from(IndexerTorrent)
                .where(
                    IndexerTorrent.is_active.is_(True),
                    IndexerTorrent.torbox_cached.is_(True),
                    IndexerTorrent.rd_cached.is_(False),
                )
            )
        ) or 0


async def drain_rd_cache_gap(batch_size: int = 20) -> int:
    """Probe Torbox-cached gaps against RD's global cache (magnet blind-add)."""
    from app.services.debrid_tokens import apply_server_debrid_tokens

    await apply_server_debrid_tokens()
    batch_size = max(1, batch_size)
    async with async_session() as db:
        items = await _magnets_for_rd_probe([], batch_size, db)
    if not items:
        return 0
    hashes = [h for h, _ in items]
    return await enrich_debrid_flags(hashes, rd_probe_limit=batch_size)


async def pending_debrid_check_count() -> int:
    async with async_session() as db:
        return (
            await db.scalar(
                select(func.count())
                .select_from(IndexerTorrent)
                .where(
                    IndexerTorrent.is_active.is_(True),
                    IndexerTorrent.last_debrid_check_at.is_(None),
                )
            )
        ) or 0


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

    Also deactivates titles that are majority non-Latin script (≥50% of letters are
    CJK / Cyrillic / Hangul / etc.) — those are blocked at upsert too so they are
    never re-recorded.

    Loads only the columns the filter needs (not full ORM rows) and flips
    is_active with set-based UPDATEs — this sweep walks the whole table.
    """
    from app.services.prowlarr import is_book_related, title_is_mostly_foreign_script
    from app.services.knaben import knaben_title_looks_like_music
    from app.services import scraper_settings
    from app.services.rss_content_filters import (
        is_too_small_for_audiobook,
        title_is_non_book,
    )

    try:
        prune_foreign = bool((await scraper_settings.get_scraper_config()).foreign_title_prune)
    except Exception:
        prune_foreign = True

    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    IndexerTorrent.id,
                    IndexerTorrent.title,
                    IndexerTorrent.indexer,
                    IndexerTorrent.media_type,
                    IndexerTorrent.size_bytes,
                ).where(IndexerTorrent.is_active.is_(True))
            )
        ).all()

        bad_ids = [
            row_id
            for row_id, title, indexer, media_type, size_bytes in rows
            if media_type not in ("audiobook", "ebook")
            or not ebook_size_acceptable(media_type, size_bytes)
            or is_too_small_for_audiobook(size_bytes, media_type)
            or title_is_non_book(title or "")
            or (prune_foreign and title_is_mostly_foreign_script(title or ""))
            or not is_book_related(
                [], title=title or "", indexer=indexer or "", media_type=media_type,
                size_bytes=int(size_bytes or 0),
            )
            or (
                "knaben" in (indexer or "").lower()
                and knaben_title_looks_like_music(title or "")
            )
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
