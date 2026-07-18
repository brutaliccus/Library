"""Runtime-tunable indexer scraper settings.

Env vars in app.config provide the defaults; overrides live in the
`app_settings` table so the admin panel can retune the scraper for the
host's capabilities (e.g. a Raspberry Pi) without a restart or redeploy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields as dataclass_fields
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import AppSetting

logger = logging.getLogger(__name__)

_PREFIX = "scraper."


@dataclass
class ScraperConfig:
    """Effective scraper tuning (env defaults merged with DB overrides)."""

    interval_seconds: int
    queries_per_job: int
    query_delay_seconds: int
    query_concurrency: int
    prowlarr_timeout: int
    search_retries: int
    abb_search_limit: int
    knaben_search_limit: int
    debrid_batch_size: int
    debrid_interval_hours: int
    prune_stale_days: int
    match_batch_size: int
    non_book_prune_every_n_jobs: int
    search_history_limit: int
    rss_every_n_jobs: int
    rss_limit_per_indexer: int
    extra_queries: str
    knaben_crawl_tasks_per_job: int
    # Mode toggles (admin UI). Defaults favor RSS maintenance + foreign prune.
    abb_rss_only: bool
    knaben_rss_only: bool
    foreign_title_prune: bool


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    description: str
    type: str  # "int" | "text" | "bool"
    min: int | None = None
    max: int | None = None


FIELDS: list[SettingField] = [
    SettingField(
        "abb_rss_only", "ABB RSS-only mode",
        "Background: only poll ABB recent-releases (no author/deep scrape, no keyword ABB crawl). "
        "Live download search still uses Jackett ABB and auto-saves finds.",
        "bool",
    ),
    SettingField(
        "knaben_rss_only", "Knaben RSS-only mode",
        "Background: only poll Knaben RSS for new uploads (skip the full category API crawl).",
        "bool",
    ),
    SettingField(
        "foreign_title_prune", "Prune foreign-script titles",
        "Drop titles that are ≥50% non-Latin script (CJK/Cyrillic/Hangul/…) from the cache "
        "and never re-record them on future scrapes.",
        "bool",
    ),
    SettingField(
        "interval_seconds", "Scrape interval (s)",
        "Pause between scrape jobs. Lower = faster crawl, more Prowlarr/CPU load.",
        "int", 5, 3600,
    ),
    SettingField(
        "queries_per_job", "Queries per job",
        "How many queue entries each job works through.",
        "int", 1, 50,
    ),
    SettingField(
        "query_delay_seconds", "Delay between query starts (s)",
        "Stagger between launching queries inside a job.",
        "int", 0, 120,
    ),
    SettingField(
        "query_concurrency", "Query concurrency",
        "Queries running against Prowlarr at the same time. Keep low on a Pi.",
        "int", 1, 8,
    ),
    SettingField(
        "prowlarr_timeout", "Prowlarr timeout (s)",
        "Max wait for a single Prowlarr search.",
        "int", 15, 300,
    ),
    SettingField(
        "search_retries", "Search retries",
        "Extra attempts when a Prowlarr search fails.",
        "int", 0, 5,
    ),
    SettingField(
        "abb_search_limit", "ABB results per query",
        "Result cap for the AudioBook Bay search.",
        "int", 25, 1000,
    ),
    SettingField(
        "knaben_search_limit", "Knaben results per query",
        "Result cap for live Knaben title searches (paginated up to 10×100 per query).",
        "int", 25, 1000,
    ),
    SettingField(
        "knaben_crawl_tasks_per_job", "Knaben crawl pages per job",
        "API pages (×100 torrents) per job while sweeping categories. Ignored when Knaben RSS-only is on. 0 = off.",
        "int", 0, 20,
    ),
    SettingField(
        "debrid_batch_size", "Debrid batch size",
        "Hashes checked against RD/Torbox per hourly batch.",
        "int", 10, 1000,
    ),
    SettingField(
        "debrid_interval_hours", "Debrid interval (h)",
        "How often the full debrid re-check batch runs.",
        "int", 1, 48,
    ),
    SettingField(
        "prune_stale_days", "Prune stale after (days)",
        "Deactivate torrents not seen on the indexers for this long.",
        "int", 1, 365,
    ),
    SettingField(
        "match_batch_size", "Catalog match batch",
        "Torrents matched against Google Books volumes per job.",
        "int", 10, 1000,
    ),
    SettingField(
        "non_book_prune_every_n_jobs", "Non-book prune every N jobs",
        "Full-table sweep for non-book noise is expensive — run it every Nth job.",
        "int", 1, 100,
    ),
    SettingField(
        "search_history_limit", "User searches in queue",
        "Recent user search queries appended to the crawl queue.",
        "int", 0, 200,
    ),
    SettingField(
        "rss_every_n_jobs", "RSS ingest every N jobs",
        "Pull each indexer's latest-releases feed every Nth job — cheap way to catch new uploads. 0 = off.",
        "int", 0, 100,
    ),
    SettingField(
        "rss_limit_per_indexer", "RSS results per indexer",
        "Result cap for each indexer's recent-releases feed.",
        "int", 25, 500,
    ),
    SettingField(
        "extra_queries", "Extra crawl queries",
        "Custom queries added to the rotation (one per line).",
        "text",
    ),
]

_FIELD_BY_KEY = {f.key: f for f in FIELDS}


def env_defaults() -> dict[str, Any]:
    s = get_settings()
    rss_n = s.scraper_rss_every_n_jobs
    if rss_n is None:
        # Default RSS cadence is every job when we ship ABB RSS-only by default.
        rss_n = 1
    return {
        "interval_seconds": s.scraper_interval_seconds,
        "queries_per_job": s.scraper_queries_per_job,
        "query_delay_seconds": s.scraper_query_delay_seconds,
        "query_concurrency": s.scraper_prowlarr_concurrency,
        "prowlarr_timeout": s.scraper_prowlarr_timeout,
        "search_retries": s.scraper_search_retries,
        "abb_search_limit": s.prowlarr_abb_search_limit,
        "knaben_search_limit": s.prowlarr_search_limit,
        "knaben_crawl_tasks_per_job": s.scraper_knaben_crawl_tasks_per_job,
        "debrid_batch_size": s.scraper_debrid_batch_size,
        "debrid_interval_hours": s.scraper_debrid_interval_hours,
        "prune_stale_days": s.scraper_prune_stale_days,
        "match_batch_size": s.scraper_match_batch_size,
        "non_book_prune_every_n_jobs": 10,
        "search_history_limit": 0,
        "rss_every_n_jobs": rss_n,
        "rss_limit_per_indexer": 100,
        "extra_queries": "",
        # Defaults ON — RSS maintenance + foreign prune. Admin can turn off.
        "abb_rss_only": True,
        "knaben_rss_only": True,
        "foreign_title_prune": True,
    }


def _coerce(field: SettingField, value: Any) -> Any:
    if field.type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if field.type == "int":
        v = int(value)
        if field.min is not None:
            v = max(field.min, v)
        if field.max is not None:
            v = min(field.max, v)
        return v
    return str(value)


# In-process cache — the scraper loop reads config every job, admin polls
# status every few seconds; no reason to hit the DB each time.
_cache: ScraperConfig | None = None


def invalidate_cache() -> None:
    global _cache
    _cache = None


async def _load_overrides() -> dict[str, Any]:
    async with async_session() as db:
        rows = (
            await db.execute(select(AppSetting).where(AppSetting.key.startswith(_PREFIX)))
        ).scalars().all()
    out: dict[str, Any] = {}
    for row in rows:
        key = row.key[len(_PREFIX):]
        field = _FIELD_BY_KEY.get(key)
        if not field:
            continue
        try:
            out[key] = _coerce(field, json.loads(row.value))
        except (ValueError, TypeError, json.JSONDecodeError):
            logger.warning("Ignoring invalid scraper setting override %s=%r", row.key, row.value)
    return out


async def get_scraper_config() -> ScraperConfig:
    global _cache
    if _cache is not None:
        return _cache
    merged = {**env_defaults(), **(await _load_overrides())}
    _cache = ScraperConfig(**merged)
    return _cache


async def update_scraper_config(updates: dict[str, Any]) -> ScraperConfig:
    """Persist valid overrides; unknown keys are rejected."""
    clean: dict[str, Any] = {}
    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if not field:
            raise ValueError(f"Unknown scraper setting: {key}")
        try:
            clean[key] = _coerce(field, value)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid value for {key}: {value!r}")

    if clean:
        async with async_session() as db:
            for key, value in clean.items():
                full_key = _PREFIX + key
                row = (
                    await db.execute(select(AppSetting).where(AppSetting.key == full_key))
                ).scalar_one_or_none()
                if row:
                    row.value = json.dumps(value)
                else:
                    db.add(AppSetting(key=full_key, value=json.dumps(value)))
            await db.commit()

    invalidate_cache()
    return await get_scraper_config()


async def reset_scraper_config() -> ScraperConfig:
    """Delete all overrides — back to env/default values."""
    from sqlalchemy import delete

    async with async_session() as db:
        await db.execute(delete(AppSetting).where(AppSetting.key.startswith(_PREFIX)))
        await db.commit()
    invalidate_cache()
    return await get_scraper_config()


def config_as_dict(cfg: ScraperConfig) -> dict[str, Any]:
    return {f.name: getattr(cfg, f.name) for f in dataclass_fields(cfg)}


def field_descriptors() -> list[dict[str, Any]]:
    return [
        {
            "key": f.key,
            "label": f.label,
            "description": f.description,
            "type": f.type,
            "min": f.min,
            "max": f.max,
        }
        for f in FIELDS
    ]
