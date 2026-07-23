"""Background indexer scraper — rotating crawl of trusted Prowlarr indexers.

All tunables come from scraper_settings (env defaults + admin-panel overrides
in the DB), so the crawl rate can be adjusted live to match the host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func, desc

from app.config import get_settings
from app.database import async_session
from app.models import ScraperState, SearchHistory, IndexerTorrent, CatalogTorrentMatch, AppSetting
from app.services import prowlarr, indexer_cache, catalog_match
from app.services import scraper_settings
from app.services.scraper_settings import ScraperConfig

logger = logging.getLogger(__name__)
settings = get_settings()

_scraper_task: asyncio.Task | None = None
_debrid_rescan_supervisor_task: asyncio.Task | None = None
_rd_gap_supervisor_task: asyncio.Task | None = None
_catalog_relink_supervisor_task: asyncio.Task | None = None
_debrid_rescan_progress: dict = {}
_catalog_relink_progress: dict = {}
_DEBRID_RESCAN_KEY = "debrid_rescan"
_CATALOG_RELINK_KEY = "catalog_relink"
_KNABEN_FULL_CRAWL_KEY = "knaben_full_crawl"
_ABB_AUTHOR_CRAWL_KEY = "abb_author_crawl"
_last_knaben_crawl_progress: dict[str, Any] = {}
_last_abb_author_progress: dict[str, Any] = {}
_last_job_indexer_counts: dict[str, int] = {"abb": 0, "knaben": 0}
_jobs_since_nonbook_prune = 0
_jobs_since_rss = 0
_jobs_since_preload = 0
_last_rss_counts: dict[str, int] = {}

# Author/A–Z deep crawl + user search history drive discovery — not broad format keywords.
# (Jackett keyword queries only return ~18 ABB hits anyway.)

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


async def _refresh_abb_author_queue() -> list[str]:
    """Top authors from the cache + A–Z singles for broad ABB discovery."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(IndexerTorrent.author_norm, func.count())
                .where(
                    IndexerTorrent.is_active.is_(True),
                    IndexerTorrent.author_norm != "",
                    IndexerTorrent.media_type == "audiobook",
                )
                .group_by(IndexerTorrent.author_norm)
                .order_by(desc(func.count()))
                .limit(400)
            )
        ).all()
    authors = [r[0] for r in rows if r[0] and len(r[0]) >= 2]
    singles = [c for c in "abcdefghijklmnopqrstuvwxyz"]
    # Singles surface authors Jackett's 2-page cap misses; names target depth.
    queue = singles + authors
    seen: set[str] = set()
    out: list[str] = []
    for q in queue:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
    return out


async def _load_abb_author_state() -> dict[str, Any]:
    async with async_session() as db:
        row = (
            await db.execute(select(AppSetting).where(AppSetting.key == _ABB_AUTHOR_CRAWL_KEY))
        ).scalar_one_or_none()
        if not row:
            return {"index": 0, "authors": await _refresh_abb_author_queue()}
        try:
            data = json.loads(row.value)
        except (ValueError, TypeError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        authors = data.get("authors") or []
        if not authors:
            authors = await _refresh_abb_author_queue()
        return {"index": int(data.get("index") or 0), "authors": authors}


async def _save_abb_author_state(state: dict[str, Any]) -> None:
    async with async_session() as db:
        row = (
            await db.execute(select(AppSetting).where(AppSetting.key == _ABB_AUTHOR_CRAWL_KEY))
        ).scalar_one_or_none()
        payload = json.dumps(state)
        if row:
            row.value = payload
        else:
            db.add(AppSetting(key=_ABB_AUTHOR_CRAWL_KEY, value=payload))
        await db.commit()


async def _run_abb_author_pass(cfg: ScraperConfig) -> tuple[int, int]:
    """One rotated author/A–Z query → multi-page FlareSolverr ABB scrape (beats Jackett 18-cap)."""
    global _last_abb_author_progress

    # Admin RSS-only (default) — no Flare deep/author crawl (avoids IP bans).
    if cfg.abb_rss_only or not getattr(settings, "abb_author_crawl_enabled", True):
        return 0, 0

    from app.services import audiobookbay

    state = await _load_abb_author_state()
    authors: list[str] = state.get("authors") or []
    if not authors:
        return 0, 0

    idx = int(state.get("index") or 0) % len(authors)
    query = authors[idx]
    state["index"] = (idx + 1) % len(authors)
    if state["index"] == 0:
        state["authors"] = await _refresh_abb_author_queue()
    await _save_abb_author_state(state)

    pages = max(2, min(6, int(getattr(settings, "abb_scraper_max_pages", 4) or 4)))
    _last_abb_author_progress = {
        "query": query,
        "index": idx,
        "queueSize": len(authors),
        "pages": pages,
    }

    try:
        deep = await audiobookbay.search_deep(query, max_pages=pages, resolve_hashes=False)
    except Exception as e:
        logger.warning("ABB author deep pass failed for %r: %s", query, e)
        return 0, 0

    if not deep:
        logger.info("ABB author pass %r: 0 listing hits (%s pages)", query, pages)
        return 0, 0

    enriched = await prowlarr.enrich_audiobookbay_for_cache(deep)
    upserted = await indexer_cache.upsert_torrents(enriched)
    logger.info(
        "ABB author pass %r: %s listings, %s upserted (%s/%s queue, %s pages)",
        query,
        len(deep),
        upserted,
        idx + 1,
        len(authors),
        pages,
    )
    return len(deep), upserted


async def _build_query_queue(cfg: ScraperConfig) -> list[str]:
    """User search history + admin extras only (no broad format-keyword rotation)."""
    queries: list[str] = []
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


async def _load_knaben_full_crawl_state():
    from app.services.knaben import crawl_state_from_json, new_full_crawl_state

    async with async_session() as db:
        row = (
            await db.execute(select(AppSetting).where(AppSetting.key == _KNABEN_FULL_CRAWL_KEY))
        ).scalar_one_or_none()
        if not row:
            return new_full_crawl_state()
        try:
            data = json.loads(row.value)
        except (ValueError, TypeError, json.JSONDecodeError):
            return new_full_crawl_state()
        return crawl_state_from_json(data if isinstance(data, dict) else None)


async def _save_knaben_full_crawl_state(state) -> None:
    from app.services.knaben import crawl_state_to_json

    async with async_session() as db:
        row = (
            await db.execute(select(AppSetting).where(AppSetting.key == _KNABEN_FULL_CRAWL_KEY))
        ).scalar_one_or_none()
        payload = json.dumps(crawl_state_to_json(state))
        if row:
            row.value = payload
        else:
            db.add(AppSetting(key=_KNABEN_FULL_CRAWL_KEY, value=payload))
        await db.commit()


async def _knaben_crawl_status(cfg: ScraperConfig) -> dict[str, Any]:
    """Persisted Knaben sweep progress for admin status."""
    state = await _load_knaben_full_crawl_state()
    summary = state.progress_summary()
    summary["pagesPerJob"] = max(0, int(cfg.knaben_crawl_tasks_per_job))
    if _last_knaben_crawl_progress:
        summary["lastBatch"] = dict(_last_knaben_crawl_progress)
    return summary


async def _run_knaben_crawl_pass(cfg: ScraperConfig) -> tuple[int, int]:
    """Exhaustive Knaben audiobook category sweep (10k-window sharding)."""
    from app.services import knaben

    global _last_knaben_crawl_progress

    if cfg.knaben_rss_only:
        return 0, 0

    pages_per_job = max(0, int(cfg.knaben_crawl_tasks_per_job))
    if pages_per_job <= 0:
        return 0, 0
    if not await prowlarr.get_knaben_indexer_ids():
        return 0, 0

    state = await _load_knaben_full_crawl_state()
    _last_knaben_crawl_progress = state.progress_summary()

    if state.phase == "maintenance":
        return 0, 0

    try:
        results = await knaben.crawl_full_category_batch(
            state,
            max_pages=pages_per_job,
            timeout=cfg.prowlarr_timeout,
        )
    except Exception as e:
        logger.warning("Knaben full crawl failed: %s", e)
        return 0, 0

    await _save_knaben_full_crawl_state(state)
    _last_knaben_crawl_progress = state.progress_summary()

    upserted = await indexer_cache.upsert_torrents(results)
    logger.info(
        "Indexer scraper Knaben full crawl: %s pages → %s results, %s upserted (%s)",
        pages_per_job,
        len(results),
        upserted,
        state.progress_summary(),
    )
    return len(results), upserted


async def _run_knaben_rss_poll(cfg: ScraperConfig) -> int:
    from app.services import knaben

    try:
        results = await knaben.poll_rss_feeds(
            size=cfg.rss_limit_per_indexer,
            timeout=cfg.prowlarr_timeout,
        )
    except Exception as e:
        logger.warning("Knaben RSS poll failed: %s", e)
        return 0

    upserted = await indexer_cache.upsert_torrents(results)
    logger.info("Indexer scraper Knaben RSS: upserted %s torrents from %s feeds", upserted, len(results))
    return upserted


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
                # RSS-only: no background keyword ABB crawl (live search still uses Jackett).
                skip_abb=cfg.abb_rss_only,
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


async def _run_rss_job(cfg: ScraperConfig) -> int:
    """Ingest each trusted indexer's latest-releases feed (Torznab empty query).

    Keyword rotation keeps re-fetching the same popular torrents; the recent
    feed is how genuinely NEW uploads enter the cache cheaply.

    When ABB RSS-only is on, also pull AudioBook Bay recent listings via
    Flare+VPN (Jackett Torznab ABB is never polled from the home IP).
    """
    global _last_rss_counts
    results, counts = await prowlarr.fetch_recent_scraper_releases(
        limit_per_indexer=cfg.rss_limit_per_indexer,
        timeout=cfg.prowlarr_timeout,
        include_abb_flare=bool(cfg.abb_rss_only),
    )
    _last_rss_counts = counts
    upserted = await indexer_cache.upsert_torrents(results)

    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one()
        state.last_rss_run_at = _utcnow()
        state.last_rss_upserted = upserted
        await db.commit()

    logger.info(
        "Indexer scraper RSS: upserted %s torrents from recent feeds (%s)",
        upserted,
        ", ".join(f"{k}={v}" for k, v in counts.items()) or "no indexers",
    )
    return upserted


async def _run_scrape_job() -> None:
    global _jobs_since_nonbook_prune, _jobs_since_rss, _jobs_since_preload

    cfg = await scraper_settings.get_scraper_config()
    state = await _get_or_create_state()
    if not state.enabled or not settings.scraper_enabled:
        return

    queries = await _build_query_queue(cfg)

    per_job = max(1, min(cfg.queries_per_job, 50)) if queries else 0
    start_idx = 0
    job_queries: list[str] = []
    if queries:
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
        elif not queries:
            logger.info("Indexer scraper: no keyword queue — author/Knaben/RSS passes only")

        crawl_hits, crawl_upserted = await _run_knaben_crawl_pass(cfg)
        job_knaben += crawl_hits
        total_upserted += crawl_upserted

        abb_hits, abb_upserted = await _run_abb_author_pass(cfg)
        job_abb += abb_hits
        total_upserted += abb_upserted

        # Recent-releases feed every Nth job (0 = disabled).
        if cfg.rss_every_n_jobs > 0:
            _jobs_since_rss += 1
            if _jobs_since_rss >= cfg.rss_every_n_jobs:
                _jobs_since_rss = 0
                try:
                    total_upserted += await _run_rss_job(cfg)
                    total_upserted += await _run_knaben_rss_poll(cfg)
                except Exception as e:
                    logger.warning("Indexer scraper RSS ingest failed: %s", e)
        else:
            # Knaben RSS when RSS cadence is off: run each job in RSS-only mode,
            # or after the full category crawl reaches maintenance.
            run_knaben_rss = cfg.knaben_rss_only
            if not run_knaben_rss:
                crawl_state = await _load_knaben_full_crawl_state()
                run_knaben_rss = crawl_state.phase == "maintenance"
            if run_knaben_rss:
                try:
                    total_upserted += await _run_knaben_rss_poll(cfg)
                except Exception as e:
                    logger.warning("Knaben RSS maintenance poll failed: %s", e)

        # One catalog match pass per job (cheaper than per query on a Pi).
        total_matches = await catalog_match.run_match_batch(cfg.match_batch_size)

        # Notify users watching books that just became cache-matched.
        if total_matches or total_upserted:
            try:
                from app.services import availability_alerts as _avail_alerts

                await _avail_alerts.notify_fulfilled_alerts()
            except Exception as e:
                logger.warning("Availability alert notify failed: %s", e)

        # Full-table non-book sweep is expensive — run it every Nth job.
        _jobs_since_nonbook_prune += 1
        if _jobs_since_nonbook_prune >= max(1, cfg.non_book_prune_every_n_jobs):
            _jobs_since_nonbook_prune = 0
            pruned = await indexer_cache.prune_non_book_torrents()
            if pruned:
                logger.info("Indexer scraper: pruned %s non-book torrents", pruned)

        # Small debrid batch after every job so RD/Torbox badges stay fresh
        mini_batch = min(100, cfg.debrid_batch_size)
        hashes = await indexer_cache.hashes_needing_debrid_check(mini_batch)
        if hashes:
            from app.services.debrid_tokens import apply_server_debrid_tokens

            await apply_server_debrid_tokens()
            updated = await indexer_cache.enrich_debrid_flags(hashes, rd_probe_limit=3)
            logger.info("Indexer scraper debrid (mini): updated %s/%s hashes", updated, len(hashes))

        # Debrid preload is expensive on a Pi (TorBox/RD HTTP + DB writes). Run
        # sparsely — continuous preload was a major contributor to overnight load spikes.
        _jobs_since_preload += 1
        if _jobs_since_preload >= 10:
            _jobs_since_preload = 0
            try:
                from app.services import debrid_preload

                preload_stats = await debrid_preload.run_preload_batch(
                    batch_size=8, poll_timeout=5, concurrency=1,
                )
                if preload_stats.get("preloaded"):
                    logger.info(
                        "Indexer scraper debrid preload: %s added",
                        preload_stats["preloaded"],
                    )
            except Exception as e:
                logger.warning("Debrid preload batch failed: %s", e)

        job_error: str | None = None
        if errors:
            job_error = "; ".join(errors)[:500]

        global _last_job_indexer_counts
        _last_job_indexer_counts = {"abb": job_abb, "knaben": job_knaben}

        async with async_session() as db:
            state = (await db.execute(select(ScraperState).limit(1))).scalar_one()
            if queries:
                state.last_query_index = (start_idx + per_job) % len(queries)
            state.last_run_at = _utcnow()
            # Only flag the job as failed when nothing succeeded — partial
            # failures keep crawling and surface the error text.
            state.status = "error" if (errors and not succeeded) else "idle"
            state.last_error = job_error
            if job_queries:
                state.last_query = job_queries[-1]
            elif _last_abb_author_progress.get("query"):
                state.last_query = f"abb:{_last_abb_author_progress['query']}"
            else:
                state.last_query = "(author/knaben/rss crawl)"
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
    from app.services.debrid_tokens import apply_server_debrid_tokens

    await apply_server_debrid_tokens()
    cfg = await scraper_settings.get_scraper_config()
    state = await _get_or_create_state()
    if not state.enabled or not settings.scraper_enabled:
        return

    batch_size = max(10, cfg.debrid_batch_size)
    hashes = await indexer_cache.hashes_needing_debrid_check(batch_size)
    if not hashes:
        return

    updated = await indexer_cache.enrich_debrid_flags(hashes, rd_probe_limit=8)
    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one()
        state.last_debrid_run_at = _utcnow()
        await db.commit()
    logger.info("Indexer scraper debrid: updated %s/%s hashes", updated, len(hashes))


async def _scraper_loop() -> None:
    last_debrid = datetime.min.replace(tzinfo=timezone.utc)

    # The matched_volumes summary table persists on disk across restarts. If it's
    # already populated, mark it ready immediately so store tabs serve from it
    # (fast) instead of the ~4s live aggregate — and DEFER the heavy rebuild so
    # it doesn't collide with the user's first requests during cold start.
    try:
        populated = await indexer_cache.mark_summary_ready_if_populated()
    except Exception as e:
        logger.warning("summary readiness check failed: %s", e)
        populated = False

    # Give the app room to serve the first cold requests before we start the
    # write-heavy scrape/refresh work (longer when we already have a warm summary).
    await asyncio.sleep(60 if populated else 8)

    try:
        await indexer_cache.refresh_matched_volumes(force=not populated)
    except Exception as e:
        logger.warning("initial matched_volumes refresh failed: %s", e)

    while True:
        try:
            cfg = await scraper_settings.get_scraper_config()
            await _run_scrape_job()
            if _utcnow() - last_debrid >= timedelta(hours=max(1, cfg.debrid_interval_hours)):
                await _run_debrid_job()
                last_debrid = _utcnow()
            await indexer_cache.prune_stale(cfg.prune_stale_days)
            # Rebuild the store's matched-volume summary so trending/browse tabs
            # stay fast instead of re-aggregating the full join per request.
            await indexer_cache.refresh_matched_volumes()
            # Subject-tag matched volumes for genre browse (incremental; cheap
            # no-op once caught up). Explicit call so initial backfill isn't
            # gated by the summary rebuild's 5-min throttle.
            try:
                await indexer_cache.refresh_volume_subjects()
            except Exception as e:
                logger.warning("volume_subjects backfill failed: %s", e)
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
        volumes_available = await db.scalar(
            select(func.count(func.distinct(CatalogTorrentMatch.google_volume_id)))
            .select_from(CatalogTorrentMatch)
            .join(IndexerTorrent, CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash)
            .where(
                CatalogTorrentMatch.match_tier.in_(("exact", "likely")),
                IndexerTorrent.is_active.is_(True),
            )
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
        checked_debrid = await db.scalar(
            select(func.count())
            .select_from(IndexerTorrent)
            .where(
                IndexerTorrent.is_active.is_(True),
                IndexerTorrent.last_debrid_check_at.isnot(None),
            )
        ) or 0
        abb_rows = await db.scalar(
            select(func.count())
            .select_from(IndexerTorrent)
            .where(
                IndexerTorrent.is_active.is_(True),
                IndexerTorrent.indexer.ilike("%audiobook%bay%"),
            )
        ) or 0
        knaben_rows = await db.scalar(
            select(func.count())
            .select_from(IndexerTorrent)
            .where(IndexerTorrent.is_active.is_(True), IndexerTorrent.indexer.ilike("%knaben%"))
        ) or 0
        total_active = await db.scalar(
            select(func.count()).select_from(IndexerTorrent).where(IndexerTorrent.is_active.is_(True))
        ) or 0
        # Only show torrents that are still active AND catalog-matched
        # (exact/likely). Raw scrape noise / pruned unmatched rows must not
        # appear in the admin "Recently cached torrents" feed.
        recent_raw = (
            await db.execute(
                select(
                    IndexerTorrent.info_hash,
                    IndexerTorrent.title,
                    IndexerTorrent.indexer,
                    IndexerTorrent.media_type,
                    IndexerTorrent.seeders,
                    IndexerTorrent.first_seen_at,
                    IndexerTorrent.rd_cached,
                    IndexerTorrent.size_bytes,
                )
                .join(
                    CatalogTorrentMatch,
                    CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash,
                )
                .where(
                    IndexerTorrent.is_active.is_(True),
                    IndexerTorrent.media_type.in_(("audiobook", "ebook")),
                    CatalogTorrentMatch.match_tier.in_(("exact", "likely")),
                )
                .order_by(desc(IndexerTorrent.first_seen_at))
                .limit(80)
            )
        ).all()
        from app.services.prowlarr import is_book_related, title_is_mostly_foreign_script
        from app.services.catalog_match import _looks_non_book
        from app.services.indexer_cache import ebook_size_acceptable

        try:
            cfg_prune = await scraper_settings.get_scraper_config()
            prune_foreign = bool(cfg_prune.foreign_title_prune)
        except Exception:
            prune_foreign = True

        recent = []
        seen_hashes: set[str] = set()
        for (
            info_hash,
            title,
            indexer,
            media_type,
            seeders,
            first_seen,
            row_rd_cached,
            size_bytes,
        ) in recent_raw:
            h = (info_hash or "").lower()
            if not h or h in seen_hashes:
                continue
            # Same gates as upsert/prune — drop false OL matches on video/junk.
            if prune_foreign and title_is_mostly_foreign_script(title or ""):
                continue
            if not ebook_size_acceptable(media_type, size_bytes):
                continue
            if _looks_non_book(title or ""):
                continue
            if not is_book_related(
                [], title=title or "", indexer=indexer or "", media_type=media_type,
                size_bytes=int(size_bytes or 0),
            ):
                continue
            seen_hashes.add(h)
            recent.append((title, indexer, media_type, seeders, first_seen, row_rd_cached))
            if len(recent) >= 12:
                break

    from app.services import debrid
    from app.services.debrid_tokens import apply_server_debrid_tokens

    await apply_server_debrid_tokens()
    debrid_providers = debrid.available_providers()

    stats = {
        "mediaTypes": {row[0] or "unknown": row[1] for row in media_rows},
        "indexers": {row[0] or "unknown": row[1] for row in indexer_rows},
        "indexersByKind": {
            "audiobookbay": int(abb_rows),
            "knaben": int(knaben_rows),
            "other": max(0, int(total_active) - int(abb_rows) - int(knaben_rows)),
        },
        "matchTiers": {row[0] or "unknown": row[1] for row in match_tier_rows},
        "catalogVolumesMatched": volumes_matched,
        "catalogVolumesAvailable": volumes_available,
        "catalogMatchesTotal": matches_total,
        "rdCached": rd_cached,
        "torboxCached": torbox_cached,
        "pendingDebridChecks": pending_debrid,
        "checkedDebridCount": checked_debrid,
        "debridProvidersConfigured": debrid_providers,
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
        from app.services import audiobookbay
        abb_health = await audiobookbay.infra_status()
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
    if cfg.abb_rss_only:
        abb_mode = "rss-only"
    elif settings.abb_deep_search_enabled:
        abb_mode = "deep"
    elif settings.abb_author_crawl_enabled:
        abb_mode = "author-crawl"
    else:
        abb_mode = "rss-only"

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
        "lastRssRunAt": state.last_rss_run_at.isoformat() if state.last_rss_run_at else None,
        "lastRssUpserted": state.last_rss_upserted or 0,
        "rssEveryNJobs": cfg.rss_every_n_jobs,
        "lastRssIndexerResults": dict(_last_rss_counts),
        "knabenCrawl": await _knaben_crawl_status(cfg),
        "abbAuthorCrawl": dict(_last_abb_author_progress),
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
        "abbMode": abb_mode,
        "abbRssOnly": bool(cfg.abb_rss_only),
        "knabenRssOnly": bool(cfg.knaben_rss_only),
        "foreignTitlePrune": bool(cfg.foreign_title_prune),
        "abbHealth": abb_health,
        "lastJobIndexerResults": dict(_last_job_indexer_counts),
        "config": scraper_settings.config_as_dict(cfg),
        "stats": {
            "mediaTypes": stats["mediaTypes"],
            "indexers": stats["indexers"],
            "matchTiers": stats["matchTiers"],
            "catalogVolumesMatched": stats["catalogVolumesMatched"],
            "catalogVolumesAvailable": stats.get("catalogVolumesAvailable", 0),
            "catalogMatchesTotal": stats["catalogMatchesTotal"],
            "rdCached": stats["rdCached"],
            "torboxCached": stats["torboxCached"],
            "pendingDebridChecks": stats["pendingDebridChecks"],
            "checkedDebridCount": stats.get("checkedDebridCount", 0),
            "debridProvidersConfigured": stats.get("debridProvidersConfigured", []),
            "indexersByKind": stats.get("indexersByKind", {}),
        },
        "recentTorrents": stats["recentTorrents"],
        "debridRescan": await get_debrid_rescan_progress_for_status(),
        "catalogRelink": await get_catalog_relink_progress_for_status(),
    }


async def clear_error() -> None:
    """Clear scraper last_error and dismiss failed debrid/relink job banners."""
    global _debrid_rescan_progress, _catalog_relink_progress

    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one_or_none()
        if state and state.status == "error":
            state.status = "idle"
            state.last_error = None
            await db.commit()

    for loader, saver, mem in (
        (_load_debrid_rescan_state, _save_debrid_rescan_state, "_debrid"),
        (_load_catalog_relink_state, _save_catalog_relink_state, "_relink"),
    ):
        try:
            job = await loader()
            if not job:
                continue
            if job.get("running"):
                continue  # don't wipe an active job
            if job.get("error"):
                job["error"] = None
                await saver(job)
                if mem == "_debrid":
                    _debrid_rescan_progress.update(job)
                else:
                    _catalog_relink_progress.update(job)
        except Exception as e:
            logger.warning("clear_error job state failed: %s", e)


async def clear_job_errors(*, force_stop: bool = False) -> dict:
    """Dismiss debrid-rescan / catalog-relink error banners (and optionally stop stuck runs)."""
    global _debrid_rescan_progress, _catalog_relink_progress

    cleared: list[str] = []
    for key, loader, saver, mem_attr in (
        ("debrid_rescan", _load_debrid_rescan_state, _save_debrid_rescan_state, "debrid"),
        ("catalog_relink", _load_catalog_relink_state, _save_catalog_relink_state, "relink"),
    ):
        job = await loader()
        if not job:
            continue
        changed = False
        if force_stop and job.get("running"):
            job["running"] = False
            changed = True
        if job.get("error"):
            job["error"] = None
            changed = True
        if changed:
            await saver(job)
            if mem_attr == "debrid":
                _debrid_rescan_progress.clear()
                _debrid_rescan_progress.update(job)
            else:
                _catalog_relink_progress.clear()
                _catalog_relink_progress.update(job)
            cleared.append(key)
    return {"cleared": cleared}


async def set_enabled(enabled: bool) -> None:
    async with async_session() as db:
        state = (await db.execute(select(ScraperState).limit(1))).scalar_one_or_none()
        if not state:
            state = ScraperState(enabled=enabled)
            db.add(state)
        else:
            state.enabled = enabled
        await db.commit()


async def refresh_debrid_cache(batch_size: int = 300) -> dict:
    """Re-check debrid instant flags using current server/library-group tokens."""
    from app.services.debrid_tokens import apply_server_debrid_tokens
    from app.database import run_with_sqlite_retry
    from sqlalchemy.exc import OperationalError

    await apply_server_debrid_tokens()

    async def _do() -> dict:
        reset = await indexer_cache.reset_stale_debrid_flags(batch_size)
        hashes = await indexer_cache.hashes_needing_debrid_check(batch_size)
        updated = await indexer_cache.enrich_debrid_flags(hashes, rd_probe_limit=3) if hashes else 0
        return {"reset": reset, "checked": len(hashes), "updated": updated}

    try:
        result = await run_with_sqlite_retry(_do, attempts=5, base_delay=1.0)
    except OperationalError as e:
        logger.warning("Debrid cache refresh skipped (database busy): %s", e)
        return {"reset": 0, "checked": 0, "updated": 0, "skipped": True, "error": str(e)[:200]}

    logger.info(
        "Debrid cache refresh: reset=%s checked=%s updated=%s",
        result["reset"],
        result["checked"],
        result["updated"],
    )
    return result


def get_debrid_rescan_progress() -> dict:
    return dict(_debrid_rescan_progress)


async def get_debrid_rescan_progress_for_status() -> dict:
    if _debrid_rescan_progress:
        return dict(_debrid_rescan_progress)
    return await _load_debrid_rescan_state()


async def _load_debrid_rescan_state() -> dict:
    from app.database import run_with_sqlite_retry

    async def _load() -> dict:
        async with async_session() as db:
            row = (
                await db.execute(select(AppSetting).where(AppSetting.key == _DEBRID_RESCAN_KEY))
            ).scalar_one_or_none()
            if not row:
                return {}
            try:
                return json.loads(row.value)
            except json.JSONDecodeError:
                return {}

    return await run_with_sqlite_retry(_load, attempts=4, base_delay=0.4)


async def _save_debrid_rescan_state(state: dict) -> None:
    from app.database import run_with_sqlite_retry

    payload = json.dumps(state)

    async def _save() -> None:
        async with async_session() as db:
            row = (
                await db.execute(select(AppSetting).where(AppSetting.key == _DEBRID_RESCAN_KEY))
            ).scalar_one_or_none()
            if row:
                row.value = payload
            else:
                db.add(AppSetting(key=_DEBRID_RESCAN_KEY, value=payload))
            await db.commit()

    await run_with_sqlite_retry(_save, attempts=8, base_delay=0.75)


async def _debrid_rescan_supervisor() -> None:
    """Runs in the main app process; resumes DB-persisted full debrid rescans."""
    from app.database import is_sqlite_lock_error
    from app.services import debrid_preload, real_debrid, torbox
    from app.services.debrid_tokens import apply_server_debrid_tokens

    global _debrid_rescan_progress
    await asyncio.sleep(3)
    _save_tick = 0
    _idle_preload_rounds = 0

    while True:
        try:
            state = await _load_debrid_rescan_state()
            if not state.get("running"):
                if _debrid_rescan_progress.get("running"):
                    _debrid_rescan_progress["running"] = False
                _idle_preload_rounds = 0
                await asyncio.sleep(3)
                continue

            _debrid_rescan_progress.update(state)
            batch_size = max(50, int(state.get("batchSize") or 300))
            # Mass preload: add magnets quickly without long polls (poll was starving progress).
            preload_batch = max(12, min(40, batch_size // 8 or 20))
            checked = int(state.get("checked") or 0)
            preloaded = int(state.get("preloaded") or 0)

            # Don't fight the scraper for SQLite — pause rescan while a scrape job holds the DB.
            scraper_state = await _get_or_create_state()
            if scraper_state.status == "running":
                await asyncio.sleep(5)
                continue

            if state.pop("requested", False):
                await apply_server_debrid_tokens()
                real_debrid.invalidate_account_cache()
                torbox.invalidate_account_cache()
                state["error"] = None
                _idle_preload_rounds = 0
                await _save_debrid_rescan_state(state)

            pending = await indexer_cache.pending_debrid_check_count()
            if pending == 0:
                # Keep preloading until accounts have the remaining magnets.
                try:
                    preload_stats = await debrid_preload.run_preload_batch(
                        batch_size=preload_batch,
                        poll_timeout=8,
                    )
                    still = int(preload_stats.get("candidates") or 0)
                    gained = int(preload_stats.get("preloaded") or 0)
                    preloaded += gained
                except Exception as e:
                    logger.warning("Debrid preload during full rescan failed: %s", e)
                    still = 0
                    gained = 0

                if still > 0 and gained == 0:
                    _idle_preload_rounds += 1
                elif gained > 0:
                    _idle_preload_rounds = 0

                # Stop when nothing left to try, or providers keep rejecting the same backlog.
                if still > 0 and _idle_preload_rounds < 8:
                    state.update(
                        {
                            "checked": checked,
                            "preloaded": preloaded,
                            "pending": 0,
                            "preloadRemaining": still,
                        }
                    )
                    _debrid_rescan_progress.update(state)
                    _save_tick += 1
                    if _save_tick % 2 == 0:
                        await _save_debrid_rescan_state(state)
                    await asyncio.sleep(2)
                    continue

                state.update(
                    {
                        "running": False,
                        "pending": 0,
                        "checked": checked,
                        "preloaded": preloaded,
                        "preloadRemaining": 0,
                        "finishedAt": _utcnow().isoformat(),
                        "error": None,
                    }
                )
                async with async_session() as db:
                    scraper_state = (
                        await db.execute(select(ScraperState).limit(1))
                    ).scalar_one_or_none()
                    if scraper_state:
                        scraper_state.last_debrid_run_at = _utcnow()
                        await db.commit()
                await _save_debrid_rescan_state(state)
                _debrid_rescan_progress.update(state)
                logger.info(
                    "Full debrid rescan complete: checked=%s preloaded=%s",
                    checked,
                    preloaded,
                )
                await asyncio.sleep(3)
                continue

            hashes = await indexer_cache.hashes_needing_debrid_check(batch_size)
            if hashes:
                updated = await indexer_cache.enrich_debrid_flags(hashes, rd_probe_limit=8)
                checked += updated
                pending = await indexer_cache.pending_debrid_check_count()
                state.update(
                    {
                        "checked": checked,
                        "pending": pending,
                    }
                )
                _debrid_rescan_progress.update(state)

            try:
                preload_stats = await debrid_preload.run_preload_batch(
                    batch_size=preload_batch,
                    poll_timeout=8,
                )
                preloaded += int(preload_stats.get("preloaded") or 0)
            except Exception as e:
                logger.warning("Debrid preload during full rescan failed: %s", e)

            pending = await indexer_cache.pending_debrid_check_count()
            state.update(
                {
                    "checked": checked,
                    "preloaded": preloaded,
                    "pending": pending,
                }
            )
            _debrid_rescan_progress.update(state)
            # Persist every other tick to cut app_settings write contention.
            _save_tick += 1
            if _save_tick % 2 == 0 or pending == 0:
                await _save_debrid_rescan_state(state)
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if is_sqlite_lock_error(e):
                logger.warning("Debrid rescan hit SQLite lock — retrying: %s", e)
                await asyncio.sleep(5)
                continue
            logger.exception("Debrid rescan supervisor error: %s", e)
            try:
                state = await _load_debrid_rescan_state()
                state["running"] = False
                state["error"] = str(e)[:500]
                await _save_debrid_rescan_state(state)
                _debrid_rescan_progress.update(state)
            except Exception as save_err:
                logger.warning("Failed to persist debrid rescan error state: %s", save_err)
            await asyncio.sleep(5)


async def _rd_gap_supervisor() -> None:
    """Background RD magnet probes for Torbox-cached torrents (RD has no global cache API)."""
    from app.services.debrid_tokens import apply_server_debrid_tokens

    await asyncio.sleep(45)
    while True:
        try:
            if not settings.scraper_enabled:
                await asyncio.sleep(60)
                continue

            state = await _get_or_create_state()
            if state.status == "running":
                await asyncio.sleep(60)
                continue

            rescan = await _load_debrid_rescan_state()
            if rescan.get("running"):
                await asyncio.sleep(90)
                continue

            gap = await indexer_cache.torbox_rd_gap_count()
            if gap < 30:
                await asyncio.sleep(180)
                continue

            await apply_server_debrid_tokens()
            batch = max(8, int(getattr(settings, "rd_gap_probe_batch", 20) or 20))
            updated = await indexer_cache.drain_rd_cache_gap(batch)
            logger.info("RD gap drain: gap=%s probed=%s updated=%s", gap, batch, updated)
            await asyncio.sleep(max(90, batch * 8))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("RD gap supervisor error: %s", e)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Full catalog re-link + prune (backfill after the Open Library ban left ~40k
# cached torrents unmatched). Resumable + DB-persisted like the debrid rescan.
# ---------------------------------------------------------------------------

def get_catalog_relink_progress() -> dict:
    return dict(_catalog_relink_progress)


async def get_catalog_relink_progress_for_status() -> dict:
    if _catalog_relink_progress:
        return dict(_catalog_relink_progress)
    return await _load_catalog_relink_state()


async def _load_catalog_relink_state() -> dict:
    from app.database import run_with_sqlite_retry

    async def _load() -> dict:
        async with async_session() as db:
            row = (
                await db.execute(select(AppSetting).where(AppSetting.key == _CATALOG_RELINK_KEY))
            ).scalar_one_or_none()
            if not row:
                return {}
            try:
                return json.loads(row.value)
            except json.JSONDecodeError:
                return {}

    return await run_with_sqlite_retry(_load, attempts=4, base_delay=0.4)


async def _save_catalog_relink_state(state: dict) -> None:
    from app.database import run_with_sqlite_retry

    payload = json.dumps(state)

    async def _save() -> None:
        async with async_session() as db:
            row = (
                await db.execute(select(AppSetting).where(AppSetting.key == _CATALOG_RELINK_KEY))
            ).scalar_one_or_none()
            if row:
                row.value = payload
            else:
                db.add(AppSetting(key=_CATALOG_RELINK_KEY, value=payload))
            await db.commit()

    await run_with_sqlite_retry(_save, attempts=8, base_delay=0.75)


async def start_catalog_relink(*, prune_unmatched: bool = True, batch_size: int = 100) -> dict:
    """Kick off a resumable full re-link of cached torrents against the local catalog."""
    global _catalog_relink_progress

    if not catalog_match.ol_catalog.catalog_ready():
        return {"ok": False, "error": "Local Open Library catalog is not built yet"}

    existing = await _load_catalog_relink_state()
    if existing.get("running"):
        _catalog_relink_progress.update(existing)
        return {
            "ok": False,
            "error": "Catalog re-link already running",
            "progress": get_catalog_relink_progress(),
        }

    async with async_session() as db:
        total = await db.scalar(
            select(func.count()).select_from(IndexerTorrent)
            .where(IndexerTorrent.is_active.is_(True))
            .where(IndexerTorrent.media_type.in_(("audiobook", "ebook")))
        ) or 0

    state = {
        "running": True,
        "cursor": 0,
        "total": int(total),
        "scanned": 0,
        "linked": 0,
        "matches": 0,
        "pruned": 0,
        "pruneUnmatched": bool(prune_unmatched),
        "batchSize": max(25, int(batch_size)),
        "startedAt": _utcnow().isoformat(),
        "error": None,
    }
    await _save_catalog_relink_state(state)
    _catalog_relink_progress.update(state)
    return {"ok": True, "progress": get_catalog_relink_progress()}


async def _catalog_relink_supervisor() -> None:
    """Runs in the app process; drives a DB-persisted full catalog re-link/prune."""
    global _catalog_relink_progress
    await asyncio.sleep(8)

    while True:
        try:
            state = await _load_catalog_relink_state()
            if not state.get("running"):
                if _catalog_relink_progress.get("running"):
                    _catalog_relink_progress["running"] = False
                await asyncio.sleep(5)
                continue

            # Only stand down for the heavy full debrid rescan (mass DB writes).
            # Normal scrape jobs are network-bound; the batch keeps its DB txns
            # short + retried so it can run alongside them.
            rescan = await _load_debrid_rescan_state()
            if rescan.get("running"):
                await asyncio.sleep(10)
                continue

            scraper_state = await _get_or_create_state()
            if scraper_state.status == "running":
                await asyncio.sleep(5)
                continue

            _catalog_relink_progress.update(state)
            batch_size = max(25, int(state.get("batchSize") or 150))
            cursor = int(state.get("cursor") or 0)

            result = await catalog_match.relink_batch(
                cursor,
                batch_size=batch_size,
                prune_unmatched=bool(state.get("pruneUnmatched", True)),
            )

            state["cursor"] = result["cursor"]
            state["scanned"] = int(state.get("scanned") or 0) + result["scanned"]
            state["linked"] = int(state.get("linked") or 0) + result["linked"]
            state["matches"] = int(state.get("matches") or 0) + result["matches"]
            state["pruned"] = int(state.get("pruned") or 0) + result["pruned"]
            state["error"] = None

            if result["done"]:
                state["running"] = False
                state["finishedAt"] = _utcnow().isoformat()
                await _save_catalog_relink_state(state)
                _catalog_relink_progress.update(state)
                logger.info(
                    "Catalog re-link complete: scanned=%s linked=%s matches=%s pruned=%s",
                    state["scanned"], state["linked"], state["matches"], state["pruned"],
                )
                await asyncio.sleep(5)
                continue

            await _save_catalog_relink_state(state)
            _catalog_relink_progress.update(state)
            # Small yield so store queries + scraper stay responsive on the Pi.
            await asyncio.sleep(0.75)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            from app.database import is_sqlite_lock_error

            if is_sqlite_lock_error(e):
                logger.warning("Catalog re-link hit SQLite lock — retrying: %s", e)
                await asyncio.sleep(5)
                continue
            logger.exception("Catalog re-link supervisor error: %s", e)
            try:
                state = await _load_catalog_relink_state()
                state["running"] = False
                state["error"] = str(e)[:500]
                await _save_catalog_relink_state(state)
                _catalog_relink_progress.update(state)
            except Exception as save_err:
                logger.warning("Failed to persist catalog re-link error state: %s", save_err)
            await asyncio.sleep(10)


async def start_full_debrid_rescan() -> dict:
    """Queue every cached torrent and re-check debrid cache + preload catalog matches."""
    global _debrid_rescan_progress

    try:
        existing = await _load_debrid_rescan_state()
        if existing.get("running"):
            _debrid_rescan_progress.update(existing)
            return {
                "ok": False,
                "error": "Full debrid rescan already running",
                "progress": get_debrid_rescan_progress(),
            }

        # Keep existing debrid IDs — clearing 30k rows hammers RD/Torbox and locks SQLite.
        queued = await indexer_cache.queue_all_debrid_recheck(clear_preload_ids=False)
        cfg = await scraper_settings.get_scraper_config()
        batch_size = max(50, cfg.debrid_batch_size)

        state = {
            "running": True,
            "requested": True,
            "queued": queued,
            "checked": 0,
            "preloaded": 0,
            "pending": queued,
            "batchSize": batch_size,
            "startedAt": _utcnow().isoformat(),
            "error": None,
        }
        await _save_debrid_rescan_state(state)
        _debrid_rescan_progress.update(state)
        return {"ok": True, "queued": queued, "batchSize": batch_size, "progress": get_debrid_rescan_progress()}
    except Exception as e:
        logger.exception("Failed to start full debrid rescan: %s", e)
        return {"ok": False, "error": str(e)[:200], "progress": get_debrid_rescan_progress()}


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
    global _scraper_task, _debrid_rescan_supervisor_task, _rd_gap_supervisor_task
    global _catalog_relink_supervisor_task
    if settings.scraper_enabled:
        if not _scraper_task or _scraper_task.done():
            _scraper_task = asyncio.create_task(_scraper_loop())
            logger.info("Indexer scraper background task started")
    else:
        logger.info("Indexer scraper disabled by config")
    if not _debrid_rescan_supervisor_task or _debrid_rescan_supervisor_task.done():
        _debrid_rescan_supervisor_task = asyncio.create_task(_debrid_rescan_supervisor())
        logger.info("Debrid rescan supervisor started")
    if not _rd_gap_supervisor_task or _rd_gap_supervisor_task.done():
        _rd_gap_supervisor_task = asyncio.create_task(_rd_gap_supervisor())
        logger.info("RD gap drain supervisor started")
    if not _catalog_relink_supervisor_task or _catalog_relink_supervisor_task.done():
        _catalog_relink_supervisor_task = asyncio.create_task(_catalog_relink_supervisor())
        logger.info("Catalog re-link supervisor started")


def stop_scraper() -> None:
    global _scraper_task, _debrid_rescan_supervisor_task, _rd_gap_supervisor_task
    global _catalog_relink_supervisor_task
    if _scraper_task and not _scraper_task.done():
        _scraper_task.cancel()
    _scraper_task = None
    if _debrid_rescan_supervisor_task and not _debrid_rescan_supervisor_task.done():
        _debrid_rescan_supervisor_task.cancel()
    _debrid_rescan_supervisor_task = None
    if _rd_gap_supervisor_task and not _rd_gap_supervisor_task.done():
        _rd_gap_supervisor_task.cancel()
    _rd_gap_supervisor_task = None
    if _catalog_relink_supervisor_task and not _catalog_relink_supervisor_task.done():
        _catalog_relink_supervisor_task.cancel()
    _catalog_relink_supervisor_task = None
