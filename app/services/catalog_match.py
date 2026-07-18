"""Link cached torrents to catalog volumes (Open Library + Google Books)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import IndexerTorrent, CatalogTorrentMatch
from app.services import google_books
from app.services import ol_catalog
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
    for field in ("isbn10", "isbn13"):
        raw = (volume.get(field) or "").strip()
        digits = "".join(c for c in raw.upper() if c.isdigit() or c == "X")
        if len(digits) in (10, 13):
            ids.add(digits)
    return ids


_FORMAT_NOISE_RE = re.compile(
    r"\b(audio\s*books?|unabridged|abridged|epub|mobi|pdf|ebook|azw3?|kindle|"
    r"mp3|m4b|m4a|ogg\s*vorbis|flac|aac|retail|scanned|hqebook|"
    r"complete\s+series|full\s+series|complete\s+collection|collection|"
    r"compilation|anthology|box\s*set|boxset|read\s+by|"
    r"svensk|dansk|norsk|deutsch|german|french|spanish|italian|russian)\b",
    re.I,
)

# Trailing scene release-group tag, e.g. "... -BitBook", "... -DiSTRiBUTiON".
_RELEASE_GROUP_RE = re.compile(r"\s*-\s*[A-Za-z0-9]{2,}\s*$")

# Positive "this is NOT a book" signals (video/music/software miscategorised as
# audiobook/ebook by the Knaben scrape). Used to gate pruning so we only drop a
# no-match entry when it clearly isn't a book — legit-but-messy or foreign-
# language books that merely fail to match stay put.
_NON_BOOK_RE = re.compile(
    r"\b(s\d{1,2}e\d{1,2}|season\s*\d+\s*(episode|ep)\b|tv\s+mini\s+series|mini\s+series|"
    r"x264|x265|h\.?264|h\.?265|hevc|xvid|divx|mpeg2|"
    r"1080p|720p|480p|2160p|4k|bluray|blu-ray|brrip|bdrip|webrip|web-dl|hdtv|dvdrip|"
    r"\d{3,4}x\d{3,4}|"
    r"discography|soundtrack|\bost\b|\bflac\b|320kbps|\bvst\b|plugin|"
    r"onlyfans|brazzers|bellesa|pornhub|xvideos|\bxxx\b|"
    r"\.iso|\.exe|\.ts\b|\.m2ts\b|\.rar\b|repack-|proper-)\b",
    re.I,
)


def _looks_non_book(title: str) -> bool:
    return bool(_NON_BOOK_RE.search(title or ""))


def _clean_release_text(raw_title: str) -> str:
    """Strip file extensions, bracket tags, years, HTML entities and format words."""
    import html

    q = html.unescape(raw_title or "")
    q = re.sub(r"\.(m4b|m4a|epub|pdf|mobi|mp3|azw3|flac|aac|ogg|iso)\b.*$", "", q, flags=re.I)
    q = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", q)
    q = re.sub(r"\b(19|20)\d{2}\b", " ", q)  # standalone years are useless + slow in FTS
    q = _FORMAT_NOISE_RE.sub(" ", q)
    q = re.sub(r"[_/]+", " ", q)
    return re.sub(r"\s+", " ", q).strip(" -,.")


def _torrent_search_queries(raw_title: str) -> list[str]:
    """Ordered catalog title guesses for a torrent release name.

    Release names come in several shapes, so we build a few likely *title*
    strings and let the caller try them in order, stopping at the first hit:
      * "Title - Author - Year" (AudiobookBay) / "Author - Title" (some Knaben):
        try each ` - ` segment.
      * "Title by Author" (no dash): try the part before " by ".
    Callers score candidates against the full release name, so an over-broad
    guess just yields no confident match rather than a wrong one.
    """
    cleaned = _clean_release_text(raw_title)
    if not cleaned:
        return []

    def _ok(s: str) -> bool:
        return len(re.sub(r"[^a-z0-9]", "", s.lower())) >= 3

    candidates: list[str] = []

    def _add(s: str) -> None:
        s = s.strip(" -,.")
        if _ok(s) and s not in candidates:
            candidates.append(s[:120])

    # Peel a trailing scene release-group tag (e.g. "-BitBook") up front.
    cleaned = _RELEASE_GROUP_RE.sub("", cleaned).strip(" -,.") or cleaned

    if " - " in cleaned:
        # Dash already separates title/author; don't also split on " by ".
        segments = [s.strip(" -") for s in cleaned.split(" - ") if s.strip(" -")]
        for seg in segments:
            _add(_RELEASE_GROUP_RE.sub("", seg))
    elif "," in cleaned and len(cleaned.split(",")) == 2:
        # "Author, Title" or "Title, Subtitle": try both parts (longer first —
        # titles are usually longer than an author name).
        parts = sorted((p.strip() for p in cleaned.split(",") if p.strip()),
                       key=len, reverse=True)
        for p in parts:
            _add(p)
        _add(cleaned)
    else:
        by_split = re.split(r"\s+by\s+", cleaned, maxsplit=1, flags=re.I)
        if len(by_split) == 2:
            _add(by_split[0])  # title before the "by Author" attribution
        _add(cleaned)

    # Cap at 3 guesses to bound FTS work per torrent.
    return candidates[:3]


def _torrent_search_query(raw_title: str) -> str:
    """Best single catalog search string (kept for callers that want one)."""
    variants = _torrent_search_queries(raw_title)
    return variants[0] if variants else ""


async def _volume_candidates_for_torrent(
    raw_title: str,
    *,
    isbn: str | None = None,
    ol_limit: int = 5,
    gb_limit: int = 3,
    conn=None,
) -> list[dict]:
    """Look up catalog volumes that might match this torrent title.

    Prefers the local Open Library catalog (built from the monthly dumps) so we
    never hit the live API during scraping. Falls back to the live API only when
    the local catalog hasn't been built yet.
    """
    queries = _torrent_search_queries(raw_title)
    if not queries:
        return []

    volumes: list[dict] = []
    seen: set[str] = set()

    def _add(book: dict) -> None:
        vid = book.get("volumeId") or book.get("id")
        if vid and vid not in seen:
            seen.add(vid)
            volumes.append(book)

    if ol_catalog.catalog_ready():
        if isbn:
            try:
                hit = await ol_catalog.lookup_isbn(isbn)
                if hit:
                    _add(hit)
            except Exception as e:
                logger.debug("catalog_match local ISBN lookup failed: %s", e)
        for q in queries:
            try:
                hits = await ol_catalog.search_by_title(q, limit=ol_limit, conn=conn)
            except Exception as e:
                logger.debug("catalog_match local OL lookup failed for %r: %s", q[:60], e)
                continue
            for b in hits:
                _add(b)
            if volumes:  # first title guess that hits is enough
                break
        # Prefer candidates that already have cover art (dump often has cover-less
        # duplicate works that otherwise win the match and blank the store card).
        volumes.sort(key=lambda b: 0 if b.get("coverUrl") else 1)
        if volumes:
            return volumes

    # ISBNdb fills gaps the local OL dump misses (commercial / recent titles).
    try:
        from app.services import isbndb

        if isbn:
            hit = await isbndb.lookup_isbn(isbn)
            if hit:
                _add(hit)
        if not volumes:
            for q in queries[:2]:
                result = await isbndb.search_books(q, limit=max(3, ol_limit))
                for b in result.get("books") or []:
                    _add(b)
                if volumes:
                    break
        if volumes:
            return volumes
    except Exception as e:
        logger.debug("catalog_match ISBNdb lookup failed: %s", e)

    # Fallback: live API (only until the local catalog is built / ISBNdb empty).
    try:
        for b in await google_books.search_open_library(queries[0], limit=ol_limit):
            _add(b)
    except Exception as e:
        logger.debug("catalog_match OL search failed for %r: %s", queries[0][:60], e)

    return volumes


async def _relink_lookup(raw_title: str, isbn: str | None, conn) -> tuple[list[dict], bool]:
    """Local-catalog lookup for the relink sweep.

    Returns ``(volumes, conclusive)``. ``conclusive`` is False when a lookup
    errored — the caller must NOT prune those (retry on a later pass) so a real
    book is never pruned because of a transient failure. When there are no title
    guesses at all (junk/too-short release name) we report conclusive=True so the
    non-book entry can be pruned.
    """
    queries = _torrent_search_queries(raw_title)
    if not queries:
        return [], True

    volumes: list[dict] = []
    seen: set[str] = set()
    conclusive = True

    def _add(book: dict) -> None:
        vid = book.get("volumeId") or book.get("id")
        if vid and vid not in seen:
            seen.add(vid)
            volumes.append(book)

    if isbn:
        try:
            hit = await ol_catalog.lookup_isbn(isbn)
            if hit:
                _add(hit)
        except Exception as e:
            conclusive = False
            logger.debug("relink ISBN lookup failed: %s", e)

    for q in queries:
        try:
            hits = await ol_catalog.search_by_title(q, limit=5, conn=conn)
        except Exception as e:
            conclusive = False
            logger.debug("relink OL lookup failed for %r: %s", q[:60], e)
            continue
        for b in hits:
            _add(b)
        if volumes:
            break

    # ISBNdb is intentionally NOT used in the full re-link sweep — it would
    # burn through the daily quota on tens of thousands of torrents. Use the
    # smaller run_match_batch / store search paths for ISBNdb enrichment.

    return volumes, conclusive


async def match_torrent_to_volumes(
    torrent: IndexerTorrent,
    volumes: list[dict],
    db: AsyncSession,
) -> int:
    """Create catalog_torrent_matches for one torrent. Returns match count."""
    created = 0
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


async def relink_batch(
    cursor: int,
    *,
    batch_size: int = 150,
    prune_unmatched: bool = True,
) -> dict:
    """One keyset-paged batch of the full catalog re-link + prune sweep.

    Walks active book torrents by ascending id (``id > cursor``). For each
    torrent that has no catalog match yet, it searches the local Open Library
    catalog and creates matches. Torrents that still have zero matches after the
    pass are deactivated when ``prune_unmatched`` is set — these are the
    miscategorised, non-book Knaben entries the store can never surface anyway
    (reversible: a later scrape that re-sees the release flips is_active back on).

    Already-matched torrents are skipped (no OL lookup) so re-running only pays
    for the unlinked backlog.

    Returns ``{cursor, scanned, linked, matches, pruned, done}`` where ``cursor``
    is the id to resume from (0-based high-water mark).
    """
    from app.database import run_with_sqlite_retry
    from sqlalchemy import update as _update

    # Phase 1 — short read txn: grab the batch's plain fields + which are matched.
    async def _load() -> dict:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(
                        IndexerTorrent.id,
                        IndexerTorrent.info_hash,
                        IndexerTorrent.title,
                        IndexerTorrent.parsed_isbn,
                    )
                    .where(IndexerTorrent.is_active.is_(True))
                    .where(IndexerTorrent.media_type.in_(("audiobook", "ebook")))
                    .where(IndexerTorrent.id > cursor)
                    .order_by(IndexerTorrent.id.asc())
                    .limit(batch_size)
                )
            ).all()
            hashes = [r.info_hash for r in rows]
            already: set[str] = set()
            for chunk in _chunk(hashes, 400):
                existing = (
                    await db.execute(
                        select(CatalogTorrentMatch.info_hash)
                        .where(CatalogTorrentMatch.info_hash.in_(chunk))
                        .distinct()
                    )
                ).scalars().all()
                already.update(existing)
            return {"rows": rows, "already": already}

    loaded = await run_with_sqlite_retry(_load, attempts=6, base_delay=1.0)
    rows = loaded["rows"]
    already: set[str] = loaded["already"]

    if not rows:
        return {"cursor": cursor, "scanned": 0, "linked": 0,
                "matches": 0, "pruned": 0, "done": True}

    # Phase 2 — NO app-DB txn held: run the local Open Library lookups on a
    # PRIVATE read-only catalog connection so we don't serialise behind (or slow
    # down) interactive store searches on the shared connection.
    #
    # We track *conclusiveness*: a torrent is only a prune candidate when its
    # lookups completed and genuinely found nothing. Lookups that error are left
    # active (retried on a later pass) so we never prune a real book by accident.
    to_write: list[tuple] = []  # (info_hash, volumes)
    prunable: list[int] = []
    ol_conn = await ol_catalog.open_private_connection()
    try:
        for r in rows:
            if r.info_hash in already:
                continue
            volumes, conclusive = await _relink_lookup(r.title, r.parsed_isbn, ol_conn)
            if volumes:
                to_write.append((r.info_hash, volumes))
            elif conclusive and prune_unmatched and _looks_non_book(r.title):
                # Only prune a no-match entry when it clearly isn't a book
                # (TV/movie/music/software noise). Legit-but-messy or foreign
                # titles that merely fail to match are kept (they just don't
                # surface in the store until matching improves / they re-scrape).
                prunable.append(r.id)
    finally:
        if ol_conn is not None:
            try:
                await ol_conn.close()
            except Exception:
                pass

    # Phase 3 — short write txn: persist matches + prune the conclusive misses.
    async def _write() -> dict:
        linked = 0
        matches = 0
        async with async_session() as db:
            for info_hash, volumes in to_write:
                t = (
                    await db.execute(
                        select(IndexerTorrent).where(IndexerTorrent.info_hash == info_hash)
                    )
                ).scalar_one_or_none()
                if t is None:
                    continue
                n = await match_torrent_to_volumes(t, volumes, db)
                if n:
                    linked += 1
                    matches += n

            for chunk in _chunk(prunable, 400):
                await db.execute(
                    _update(IndexerTorrent)
                    .where(IndexerTorrent.id.in_(chunk))
                    .values(is_active=False)
                )

            await db.commit()
            return {"linked": linked, "matches": matches, "pruned": len(prunable)}

    written = await run_with_sqlite_retry(_write, attempts=6, base_delay=1.0)
    return {
        "cursor": rows[-1].id,
        "scanned": len(rows),
        "linked": written["linked"],
        "matches": written["matches"],
        "pruned": written["pruned"],
        "done": len(rows) < batch_size,
    }


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def run_match_batch(batch_size: int = 50) -> int:
    """Match recently seen book torrents by searching Open Library / ISBNdb per title.

    Uses short DB transactions (read batch → lookups with no app DB held → write)
    so concurrent scraper/rescan work does not exhaust the connection pool.
    """
    from app.database import run_with_sqlite_retry

    async def _load() -> list:
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
            return [(t.info_hash, t.title, t.parsed_isbn) for t in torrents]

    loaded = await run_with_sqlite_retry(_load, attempts=4, base_delay=0.5)
    if not loaded:
        return 0

    planned: list[tuple[str, list[dict]]] = []
    for info_hash, title, isbn in loaded:
        volumes = await _volume_candidates_for_torrent(title, isbn=isbn)
        if volumes:
            planned.append((info_hash, volumes))

    if not planned:
        logger.info("catalog_match batch: %s torrents scanned, 0 matches", len(loaded))
        return 0

    async def _write() -> int:
        total = 0
        matched_torrents = 0
        async with async_session() as db:
            hashes = [h for h, _ in planned]
            rows = (
                await db.execute(
                    select(IndexerTorrent).where(IndexerTorrent.info_hash.in_(hashes))
                )
            ).scalars().all()
            by_hash = {r.info_hash: r for r in rows}
            for info_hash, volumes in planned:
                t = by_hash.get(info_hash)
                if not t:
                    continue
                n = await match_torrent_to_volumes(t, volumes, db)
                if n:
                    matched_torrents += 1
                    total += n
            await db.commit()
        logger.info(
            "catalog_match batch: %s torrents scanned, %s had matches, %s match rows touched",
            len(loaded),
            matched_torrents,
            total,
        )
        return total

    return await run_with_sqlite_retry(_write, attempts=6, base_delay=1.0)
