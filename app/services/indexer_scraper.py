"""Background indexer scraper — rotating crawl of trusted Prowlarr indexers.

All tunables come from scraper_settings (env defaults + admin-panel overrides
in the DB), so the crawl rate can be adjusted live to match the host.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, desc

from app.config import get_settings
from app.database import async_session
from app.models import ScraperState, SearchHistory, IndexerTorrent, CatalogTorrentMatch
from app.services import prowlarr, indexer_cache, catalog_match
from app.services import scraper_settings
from app.services.scraper_settings import ScraperConfig

logger = logging.getLogger(__name__)
settings = get_settings()

_scraper_task: asyncio.Task | None = None
_last_job_indexer_counts: dict[str, int] = {"abb": 0, "knaben": 0}
_jobs_since_nonbook_prune = 0

# Book-focused queries first; no single-letter noise (wastes Prowlarr time on Knaben junk).
_BASE_QUERIES: list[str] = [
    "fantasy audiobook", "sci-fi audiobook", "mystery audiobook", "thriller audiobook",
    "romance audiobook", "horror audiobook", "history audiobook", "biography audiobook",
    "stephen king", "brandon sanderson", "james patterson", "lee child", "agatha christie",
    "fantasy epub", "sci-fi epub", "romance epub", "mystery epub",
    "unabridged", "audiobook", "m4b", "epub", "pdf", "mobi",
    "fantasy", "sci-fi", "romance", "mystery", "thriller", "horror",
    "history", "biography", "self help", "business",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _get_or_create_state() -> ScraperState:
    async with async_session() as db:
        row = (await db.execute(select(ScraperState).limit(1))).scalar_one_or_none()
        if not row:
            row = ScraperState()
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row


def _extra_queries(cfg: ScraperConfig) -> list[str]:
    out: list[str] = []
    for line in (cfg.extra_queries or "").replace(",", "\n").splitlines():
        q = line.strip()
        if len(q) >= 2 and q not in out:
            out.append(q)
    return out


async def _build_query_queue(cfg: ScraperConfig) -> list[str]:
    queries = list(_BASE_QUERIES)
    for q in _extra_queries(cfg):
        if q not in queries:
            queries.append(q)

    history_limit = max(0, cfg.search_history_limit)
    if history_limit:
        async with async_session() as db:
            hist = (
                await db.execute(
                    select(SearchHistory.query)
                    .order_by(SearchHistory.created_at.desc())
                    .limit(history_limit)
                )
            ).scalars().all()
        for q in hist:
            q = (q or "").strip()
            if len(q) >= 2 and q not in queries:
                queries.append(q)
    return queries


async def _search_with_retry(query: str, cfg: ScraperConfig) -> tuple[list[dict], dict[str, int]]:
    """Prowlarr search with short retries; ABB and Knaben queried separately."""
    retries = max(0, cfg.search_retries)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await prowlarr.search_scraper_indexers(
                query,
                abb_limit=cfg.abb_search_limit,
                knaben_limit=cfg.knaben_search_limit,
                timeout=cfg.prowlarr_timeout,
            )
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 8 * (attempt + 1)
                logger.warning(
                    "Indexer scraper: query %r failed (attempt %s/%s), retry in %ss: %s",
                    query, attempt + 1, retries + 1, wait, e,
                )
                await asyncio.sleep(wait)
    assert last_err is not None
    raise last_err


async def _run_scrape_job() -> None:
    global _jobs_since_nonbook_prune

    cfg = await scraper_settings.get_scraper_config()
    state = await _get_or_create_state()
    if not state.enabled or not settings.scraper_enabled:
        return

    queries = await _build_query_queue(cfg)
    if not queries:
        return

    per_job = max(1, min(cfg.queries_per_job, 50))
    start_idx = state.last_query_index % len(queries)
    job_queries = [queries[(start_idx + i) % len(queries)] for i in range(per_job)]

    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one()
        state.status = "running"
        state.last_error = None
        await db.commit()

    total_upserted = 0
    total_matches = 0
    job_abb = 0
    job_knaben = 0
    errors: list[str] = []
    succeeded = 0

    # Queries run concurrently up to query_concurrency; the DB lock keeps
    # SQLite happy with a single writer at a time.
    sem = asyncio.Semaphore(max(1, cfg.query_concurrency))
    db_lock = asyncio.Lock()

    async def run_one(query: str) -> None:
        nonlocal total_upserted, job_abb, job_knaben, succeeded
        async with sem:
            try:
                results, idx_counts = await _search_with_retry(query, cfg)
            except Exception as e:
                errors.append(f"{query!r}: {str(e)[:200]}")
                logger.exception("Indexer scraper: query %r failed: %s", query, e)
                return
        job_abb += idx_counts.get("abb", 0)
        job_knaben += idx_counts.get("knaben", 0)
        async with db_lock:
            count = await indexer_cache.upsert_torrents(results)
        total_upserted += count
        succeeded += 1
        logger.info("Indexer scraper: upserted %s torrents from %r", count, query)

    try:
        tasks: list[asyncio.Task] = []
        for i, query in enumerate(job_queries):
            if i and cfg.query_delay_seconds:
                await asyncio.sleep(cfg.query_delay_seconds)
            logger.info(
                "Indexer scraper: query %r (job %s/%s)", query, i + 1, per_job
            )
            tasks.append(asyncio.create_task(run_one(query)))
        if tasks:
            await asyncio.gather(*tasks)

        # One catalog match pass per job (cheaper than per query on a Pi).
        total_matches = await catalog_match.run_match_batch(cfg.match_batch_size)

        # Full-table non-book sweep is expensive — run it every Nth job.
        _jobs_since_nonbook_prune += 1
        if _jobs_since_nonbook_prune >= max(1, cfg.non_book_prune_every_n_jobs):
            _jobs_since_nonbook_prune = 0
            pruned = await indexer_cache.prune_non_book_torrents()
            if pruned:
                logger.info("Indexer scraper: pruned %s non-book torrents", pruned)

        # Small debrid batch after every job so RD badges stay fresh
        mini_batch = min(100, cfg.debrid_batch_size)
        hashes = await indexer_cache.hashes_needing_debrid_check(mini_batch)
        if hashes:
            updated = await indexer_cache.enrich_debrid_flags(hashes)
            logger.info("Indexer scraper debrid (mini): updated %s/%s hashes", updated, len(hashes))

        job_error: str | None = None
        if errors:
            job_error = "; ".join(errors)[:500]

        global _last_job_indexer_counts
        _last_job_indexer_counts = {"abb": job_abb, "knaben": job_knaben}

        async with async_session() as db:
            state = (await db.execute(select(ScraperState).limit(1))).scalar_one()
            state.last_query_index = (start_idx + per_job) % len(queries)
            state.last_run_at = _utcnow()
            # Only flag the job as failed when nothing succeeded — partial
            # failures keep crawling and surface the error text.
            state.status = "error" if (errors and not succeeded) else "idle"
            state.last_error = job_error
            state.last_query = job_queries[-1]
            state.last_upserted_count = total_upserted
            state.last_matches_created = total_matches
            state.torrents_total = await db.scalar(
                select(func.count()).select_from(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
            ) or 0
            await db.commit()

        if errors and not succeeded:
            raise RuntimeError(job_error)

    except Exception as e:
        async with async_session() as db:
            state = (await db.execute(select(ScraperState).limit(1))).scalar_one()
            state.status = "error"
            state.last_error = str(e)[:500]
            await db.commit()
        raise


async def _run_debrid_job() -> None:
    cfg = await scraper_settings.get_scraper_config()
    state = await _get_or_create_state()
    if not state.enabled or not settings.scraper_enabled:
        return

    batch_size = max(10, cfg.debrid_batch_size)
    hashes = await indexer_cache.hashes_needing_debrid_check(batch_size)
    if not hashes:
        return

    updated = await indexer_cache.enrich_debrid_flags(hashes)
    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one()
        state.last_debrid_run_at = _utcnow()
        await db.commit()
    logger.info("Indexer scraper debrid: updated %s/%s hashes", updated, len(hashes))


async def _scraper_loop() -> None:
    await asyncio.sleep(5)
    last_debrid = datetime.min.replace(tzinfo=timezone.utc)

    while True:
        try:
            cfg = await scraper_settings.get_scraper_config()
            await _run_scrape_job()
            if _utcnow() - last_debrid >= timedelta(hours=max(1, cfg.debrid_interval_hours)):
                await _run_debrid_job()
                last_debrid = _utcnow()
            await indexer_cache.prune_stale(cfg.prune_stale_days)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Indexer scraper loop error: %s", e)
        # Re-read the interval every cycle so admin changes apply immediately.
        try:
            cfg = await scraper_settings.get_scraper_config()
            interval = max(5, cfg.interval_seconds)
        except Exception:
            interval = max(20, settings.scraper_interval_seconds)
        await asyncio.sleep(interval)


# Aggregate stats are ~10 queries; the admin panel polls every 3–15s, which
# adds up on a Pi. Cache them briefly.
_STATS_TTL_SECONDS = 10
_stats_cache: tuple[float, dict] | None = None


async def _collect_stats() -> dict:
    """DB stats for admin dashboard; isolated so get_status can degrade gracefully."""
    global _stats_cache
    if _stats_cache and time.monotonic() - _stats_cache[0] < _STATS_TTL_SECONDS:
        return _stats_cache[1]

    async with async_session() as db:
        media_rows = (
            await db.execute(
                select(IndexerTorrent.media_type, func.count())
                .where(IndexerTorrent.is_active.is_(True))
                .group_by(IndexerTorrent.media_type)
            )
        ).all()
        indexer_rows = (
            await db.execute(
                select(IndexerTorrent.indexer, func.count())
                .where(IndexerTorrent.is_active.is_(True))
                .group_by(IndexerTorrent.indexer)
                .order_by(desc(func.count()))
                .limit(8)
            )
        ).all()
        match_tier_rows = (
            await db.execute(
                select(CatalogTorrentMatch.match_tier, func.count())
                .group_by(CatalogTorrentMatch.match_tier)
            )
        ).all()
        volumes_matched = await db.scalar(
            select(func.count(func.distinct(CatalogTorrentMatch.google_volume_id)))
        ) or 0
        matches_total = await db.scalar(select(func.count()).select_from(CatalogTorrentMatch)) or 0
        rd_cached = await db.scalar(
            select(func.count())
            .select_from(IndexerTorrent)
            .where(IndexerTorrent.is_active.is_(True), IndexerTorrent.rd_cached.is_(True))
        ) or 0
        torbox_cached = await db.scalar(
            select(func.count())
            .select_from(IndexerTorrent)
            .where(IndexerTorrent.is_active.is_(True), IndexerTorrent.torbox_cached.is_(True))
        ) or 0
        pending_debrid = await db.scalar(
            select(func.count())
            .select_from(IndexerTorrent)
            .where(IndexerTorrent.is_active.is_(True), IndexerTorrent.last_debrid_check_at.is_(None))
        ) or 0
        recent = (
            await db.execute(
                select(
                    IndexerTorrent.title,
                    IndexerTorrent.indexer,
                    IndexerTorrent.media_type,
                    IndexerTorrent.seeders,
                    IndexerTorrent.first_seen_at,
                    IndexerTorrent.rd_cached,
                )
                .where(IndexerTorrent.is_active.is_(True))
                .order_by(desc(IndexerTorrent.first_seen_at))
                .limit(12)
            )
        ).all()

    stats = {
        "mediaTypes": {row[0] or "unknown": row[1] for row in media_rows},
        "indexers": {row[0] or "unknown": row[1] for row in indexer_rows},
        "matchTiers": {row[0] or "unknown": row[1] for row in match_tier_rows},
        "catalogVolumesMatched": volumes_matched,
        "catalogMatchesTotal": matches_total,
        "rdCached": rd_cached,
        "torboxCached": torbox_cached,
        "pendingDebridChecks": pending_debrid,
        "recentTorrents": [
            {
                "title": row[0],
                "indexer": row[1],
                "mediaType": row[2],
                "seeders": row[3],
                "firstSeenAt": row[4].isoformat() if row[4] else None,
                "rdCached": bool(row[5]),
            }
            for row in recent
        ],
    }
    _stats_cache = (time.monotonic(), stats)
    return stats


async def get_status() -> dict:
    try:
        cfg = await scraper_settings.get_scraper_config()
        state = await _get_or_create_state()
        total = await indexer_cache.torrent_count()
        queries = await _build_query_queue(cfg)
        idx = state.last_query_index % len(queries) if queries else 0
        current_query = queries[idx] if queries else ""
        queue_progress = round((idx / len(queries)) * 100, 1) if queries else 0
        stats = await _collect_stats()
        configured = await prowlarr.get_trusted_indexer_info()
    except Exception as e:
        logger.exception("scraper get_status failed: %s", e)
        return {
            "enabled": settings.scraper_enabled,
            "configEnabled": settings.scraper_enabled,
            "dbEnabled": True,
            "status": "error",
            "torrentsTotal": 0,
            "lastError": f"Status check failed: {e}"[:500],
            "intervalSeconds": settings.scraper_interval_seconds,
            "queriesPerJob": settings.scraper_queries_per_job,
            "stats": {
                "mediaTypes": {},
                "indexers": {},
                "matchTiers": {},
                "catalogVolumesMatched": 0,
                "catalogMatchesTotal": 0,
                "rdCached": 0,
                "torboxCached": 0,
                "pendingDebridChecks": 0,
            },
            "recentTorrents": [],
            "configuredIndexers": [],
        }

    abb_configured = any(i["kind"] == "audiobookbay" for i in configured)
    knaben_configured = any(i["kind"] == "knaben" for i in configured)

    next_queries = []
    if queries:
        for offset in range(1, 6):
            next_queries.append(queries[(idx + offset) % len(queries)])

    interval = max(5, cfg.interval_seconds)
    next_run_at = None
    if state.last_run_at and state.status in ("idle", "error"):
        next_run = state.last_run_at + timedelta(seconds=interval)
        next_run_at = next_run.isoformat()

    per_job = max(1, cfg.queries_per_job)
    queries_per_hour = round(3600 / interval * per_job, 1)

    return {
        "enabled": state.enabled and settings.scraper_enabled,
        "configEnabled": settings.scraper_enabled,
        "dbEnabled": state.enabled,
        "status": state.status,
        "torrentsTotal": total,
        "lastRunAt": state.last_run_at.isoformat() if state.last_run_at else None,
        "lastDebridRunAt": state.last_debrid_run_at.isoformat() if state.last_debrid_run_at else None,
        "lastQueryIndex": state.last_query_index,
        "queryQueueSize": len(queries),
        "currentQuery": current_query,
        "queueProgressPercent": queue_progress,
        "nextQueries": next_queries,
        "nextRunAt": next_run_at,
        "lastError": state.last_error,
        "lastQuery": state.last_query,
        "lastUpsertedCount": state.last_upserted_count or 0,
        "lastMatchesCreated": state.last_matches_created or 0,
        "intervalSeconds": interval,
        "queriesPerJob": per_job,
        "queriesPerHour": queries_per_hour,
        "debridIntervalHours": cfg.debrid_interval_hours,
        "debridBatchSize": cfg.debrid_batch_size,
        "matchBatchSize": cfg.match_batch_size,
        "configuredIndexers": configured,
        "abbConfigured": abb_configured,
        "knabenConfigured": knaben_configured,
        "lastJobIndexerResults": dict(_last_job_indexer_counts),
        "config": scraper_settings.config_as_dict(cfg),
        "stats": {
            "mediaTypes": stats["mediaTypes"],
            "indexers": stats["indexers"],
            "matchTiers": stats["matchTiers"],
            "catalogVolumesMatched": stats["catalogVolumesMatched"],
            "catalogMatchesTotal": stats["catalogMatchesTotal"],
            "rdCached": stats["rdCached"],
            "torboxCached": stats["torboxCached"],
            "pendingDebridChecks": stats["pendingDebridChecks"],
        },
        "recentTorrents": stats["recentTorrents"],
    }


async def clear_error() -> None:
    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one_or_none()
        if state and state.status == "error":
            state.status = "idle"
            state.last_error = None
            await db.commit()


async def set_enabled(enabled: bool) -> None:
    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one_or_none()
        if not state:
            state = ScraperState(enabled=enabled)
            db.add(state)
        else:
            state.enabled = enabled
        await db.commit()


async def trigger_scrape_now() -> dict:
    """Run one scrape job immediately (admin). Returns error if already running."""
    state = await _get_or_create_state()
    if state.status == "running":
        return {"ok": False, "error": "Scraper is already running"}
    if not state.enabled or not settings.scraper_enabled:
        return {"ok": False, "error": "Scraper is disabled"}
    try:
        await _run_scrape_job()
    except Exception as e:
        return {"ok": False, "error": str(e)[:500], "status": await get_status()}
    return {"ok": True, "status": await get_status()}


def start_scraper() -> None:
    global _scraper_task
    if not settings.scraper_enabled:
        logger.info("Indexer scraper disabled by config")
        return
    if _scraper_task and not _scraper_task.done():
        return
    _scraper_task = asyncio.create_task(_scraper_loop())
    logger.info("Indexer scraper background task started")


def stop_scraper() -> None:
    global _scraper_task
    if _scraper_task and not _scraper_task.done():
        _scraper_task.cancel()
    _scraper_task = None
