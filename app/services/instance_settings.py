"""Unified instance configuration registry.

Editable settings live in ``app_settings`` (DB) with env-var fallbacks from
``Settings``. Paths / SECRET_KEY / Docker-internal URLs stay env-only and are
exposed as read-only in the Admin Config UI.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from app.config import get_settings
from app.services import app_settings

logger = logging.getLogger(__name__)

ValueType = Literal["string", "secret", "bool", "int", "float", "text"]

# Short process cache so hot paths (Kavita/ABS requests) don't hit SQLite every call.
_eff_cache: dict[str, tuple[float, str]] = {}
_EFF_TTL = 30.0


@dataclass(frozen=True)
class SettingDef:
    key: str
    group: str
    label: str
    env_attr: str = ""
    value_type: ValueType = "string"
    secret: bool = False
    editable: bool = True
    restart_required: bool = False
    high_usage: bool = False
    help: str = ""
    placeholder: str = ""
    # When True, empty DB override falls back to env; clearing DB restores env.
    env_fallback: bool = True


REGISTRY: list[SettingDef] = [
    # --- Core ---
    SettingDef(
        key="config.app_url",
        group="core",
        label="App URL",
        env_attr="app_url",
        help="Public HTTPS URL of this Library (invite links, CORS, push). Must be the URL friends open — not localhost.",
        restart_required=True,
        placeholder="https://library.example.com",
    ),
    SettingDef(
        key="config.secret_key",
        group="core",
        label="Secret key",
        env_attr="secret_key",
        secret=True,
        editable=False,
        restart_required=True,
        help="JWT signing secret — set in .env only, never exposed in full.",
    ),
    # --- Libraries ---
    SettingDef(
        key="config.abs_url",
        group="libraries",
        label="Audiobookshelf URL",
        env_attr="abs_url",
        placeholder="http://192.168.1.10:13378",
    ),
    SettingDef(
        key="config.abs_api_key",
        group="libraries",
        label="Audiobookshelf API key",
        env_attr="abs_api_key",
        secret=True,
    ),
    SettingDef(
        key="config.abs_library_id",
        group="libraries",
        label="Audiobookshelf library ID",
        env_attr="abs_library_id",
    ),
    SettingDef(
        key="config.kavita_url",
        group="libraries",
        label="Kavita URL",
        env_attr="kavita_url",
        placeholder="http://192.168.1.10:5000",
    ),
    SettingDef(
        key="config.kavita_api_key",
        group="libraries",
        label="Kavita API key",
        env_attr="kavita_api_key",
        secret=True,
    ),
    SettingDef(
        key="config.kavita_library_id",
        group="libraries",
        label="Kavita library ID",
        env_attr="kavita_library_id",
        value_type="int",
        help="0 = use default / first ebook library.",
    ),
    # --- Indexers ---
    SettingDef(
        key="config.prowlarr_url",
        group="indexers",
        label="Prowlarr URL",
        env_attr="prowlarr_url",
        placeholder="http://prowlarr:9696",
    ),
    SettingDef(
        key="config.prowlarr_api_key",
        group="indexers",
        label="Prowlarr API key",
        env_attr="prowlarr_api_key",
        secret=True,
    ),
    SettingDef(
        key="config.jackett_url",
        group="indexers",
        label="Jackett URL",
        env_attr="jackett_url",
        placeholder="http://audiobook-jackett:9117",
    ),
    SettingDef(
        key="config.jackett_api_key",
        group="indexers",
        label="Jackett API key",
        env_attr="jackett_api_key",
        secret=True,
        help="Usually synced automatically from the Jackett container.",
    ),
    SettingDef(
        key="config.flaresolverr_url",
        group="indexers",
        label="FlareSolverr URL",
        env_attr="flaresolverr_url",
        placeholder="http://flaresolverr:8191",
        help="Used for AudioBook Bay challenge bypass (high CPU on a Pi).",
    ),
    # --- Debrid (server defaults / Main Library fallback) ---
    SettingDef(
        key="config.real_debrid_api_token",
        group="debrid",
        label="Real-Debrid API token (server default)",
        env_attr="real_debrid_api_token",
        secret=True,
        help="Fallback when a library group has no key of its own.",
    ),
    SettingDef(
        key="config.torbox_api_token",
        group="debrid",
        label="TorBox API token (server default)",
        env_attr="torbox_api_token",
        secret=True,
    ),
    # --- Catalog APIs ---
    SettingDef(
        key="integrations.hardcover_api_key",
        group="catalog",
        label="Hardcover API key",
        env_attr="hardcover_api_key",
        secret=True,
        help="Ratings, series graphs, curated lists. https://hardcover.app/account/api",
    ),
    SettingDef(
        key="integrations.nyt_api_key",
        group="catalog",
        label="NYT Books API key",
        env_attr="nyt_api_key",
        secret=True,
        help="Real bestsellers for Trending. Free at developer.nytimes.com",
    ),
    SettingDef(
        key="integrations.isbndb_api_key",
        group="catalog",
        label="ISBNdb API key",
        env_attr="isbndb_api_key",
        secret=True,
    ),
    SettingDef(
        key="config.google_books_api_key",
        group="catalog",
        label="Google Books API key",
        env_attr="google_books_api_key",
        secret=True,
        help="Optional — improves genre browse / metadata fallbacks.",
    ),
    SettingDef(
        key="config.aa_account_id",
        group="catalog",
        label="Anna's Archive membership cookie",
        env_attr="aa_account_id",
        secret=True,
        value_type="text",
        help="Optional membership cookie for faster AA ebook downloads.",
    ),
    # --- VPN ---
    SettingDef(
        key="integrations.mullvad_account_number",
        group="vpn",
        label="Mullvad account number",
        env_attr="mullvad_account_number",
        secret=True,
        help="ABB-only traffic via gluetun. Saving auto-registers WireGuard keys.",
    ),
    SettingDef(
        key="config.abb_proxy_url",
        group="vpn",
        label="ABB HTTP proxy URL",
        env_attr="abb_proxy_url",
        placeholder="http://gluetun:8888",
        help="Usually gluetun:8888. Leave empty to disable ABB proxying.",
    ),
    # --- Push ---
    SettingDef(
        key="config.vapid_public_key",
        group="notifications",
        label="VAPID public key",
        env_attr="vapid_public_key",
        restart_required=True,
        help="Web Push. Generate with scripts/generate_vapid.py",
    ),
    SettingDef(
        key="config.vapid_private_key",
        group="notifications",
        label="VAPID private key",
        env_attr="vapid_private_key",
        secret=True,
        editable=False,
        restart_required=True,
        help="Set in .env only (PEM). Not editable from the UI.",
    ),
    # --- Scraper high-usage toggles (also in Cache tab; mirrored here) ---
    SettingDef(
        key="scraper.abb_rss_only",
        group="scraper",
        label="ABB RSS-only mode",
        value_type="bool",
        high_usage=True,
        help="Recommended on a Pi. When on, no FlareSolverr author/deep crawl — RSS + live Jackett search only.",
        env_fallback=False,
    ),
    SettingDef(
        key="scraper.knaben_rss_only",
        group="scraper",
        label="Knaben RSS-only mode",
        value_type="bool",
        high_usage=True,
        help="Recommended default. When on, skip full Knaben category crawl.",
        env_fallback=False,
    ),
    SettingDef(
        key="config.abb_author_crawl_enabled",
        group="scraper",
        label="ABB author / A–Z deep crawl",
        env_attr="abb_author_crawl_enabled",
        value_type="bool",
        high_usage=True,
        help="HIGH USAGE — FlareSolverr multi-page crawl. Keep off unless you know you need it.",
    ),
    SettingDef(
        key="config.abb_live_search_enabled",
        group="scraper",
        label="ABB live Flare deep search",
        env_attr="abb_live_search_enabled",
        value_type="bool",
        high_usage=True,
        help="HIGH USAGE — multi-page Flare during user searches. Jackett-first search works without this.",
    ),
    SettingDef(
        key="config.scraper_enabled",
        group="scraper",
        label="Scraper master enable (env)",
        env_attr="scraper_enabled",
        value_type="bool",
        editable=False,
        restart_required=True,
        help="Env kill switch. Runtime on/off is Admin → Cache.",
    ),
    # --- Storage (read-only) ---
    SettingDef(
        key="config.audiobook_dir",
        group="storage",
        label="Audiobook directory",
        env_attr="audiobook_dir",
        editable=False,
        restart_required=True,
    ),
    SettingDef(
        key="config.ebook_dir",
        group="storage",
        label="Ebook directory",
        env_attr="ebook_dir",
        editable=False,
        restart_required=True,
    ),
    SettingDef(
        key="config.ol_catalog_db_path",
        group="storage",
        label="Open Library catalog DB",
        env_attr="ol_catalog_db_path",
        editable=False,
        restart_required=True,
    ),
]

GROUPS: list[dict[str, str]] = [
    {"id": "core", "label": "Core"},
    {"id": "libraries", "label": "Libraries (ABS / Kavita)"},
    {"id": "indexers", "label": "Indexers"},
    {"id": "debrid", "label": "Debrid (server defaults)"},
    {"id": "catalog", "label": "Catalog APIs"},
    {"id": "vpn", "label": "VPN / ABB proxy"},
    {"id": "notifications", "label": "Push notifications"},
    {"id": "scraper", "label": "Scraper / discovery"},
    {"id": "storage", "label": "Storage paths"},
]

_BY_KEY = {d.key: d for d in REGISTRY}


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return ("*" * max(0, len(secret) - 4)) + secret[-4:]


def _env_value(attr: str) -> str:
    if not attr:
        return ""
    s = get_settings()
    val = getattr(s, attr, None)
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def invalidate_cache(key: str | None = None) -> None:
    if key is None:
        _eff_cache.clear()
    else:
        _eff_cache.pop(key, None)


async def get_effective(key: str) -> str:
    """DB override if set, else env. Cached briefly for hot paths."""
    now = time.monotonic()
    hit = _eff_cache.get(key)
    if hit and now - hit[0] < _EFF_TTL:
        return hit[1]

    defn = _BY_KEY.get(key)
    stored = await app_settings.get_setting(key, default="")
    if stored:
        value = stored
    elif defn and defn.env_fallback and defn.env_attr:
        value = _env_value(defn.env_attr)
    else:
        # scraper.* bools without env: read scraper_settings defaults
        value = stored

    # Special-case scraper bools that live in scraper_settings merge
    if key in ("scraper.abb_rss_only", "scraper.knaben_rss_only") and not stored:
        try:
            from app.services import scraper_settings as ss

            cfg = await ss.get_scraper_config()
            if key == "scraper.abb_rss_only":
                value = "true" if cfg.abb_rss_only else "false"
            else:
                value = "true" if cfg.knaben_rss_only else "false"
        except Exception:
            value = "true"

    _eff_cache[key] = (now, value)
    return value


async def get_effective_bool(key: str, default: bool = False) -> bool:
    raw = (await get_effective(key)).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _docker_host_fix(url: str) -> str:
    from app.config import _host_for_docker
    import os

    if os.path.exists("/.dockerenv"):
        return _host_for_docker(url)
    return url


async def get_abs_connection() -> tuple[str, str, str]:
    url = _docker_host_fix(await get_effective("config.abs_url"))
    key = await get_effective("config.abs_api_key")
    lib = await get_effective("config.abs_library_id")
    return url, key, lib


async def get_kavita_connection() -> tuple[str, str, int]:
    url = _docker_host_fix(await get_effective("config.kavita_url"))
    key = await get_effective("config.kavita_api_key")
    lib_raw = await get_effective("config.kavita_library_id")
    try:
        lib = int(lib_raw or "0")
    except ValueError:
        lib = 0
    return url, key, lib


async def get_prowlarr_connection() -> tuple[str, str]:
    return (
        await get_effective("config.prowlarr_url"),
        await get_effective("config.prowlarr_api_key"),
    )


async def list_config(*, reveal_secrets: bool = False) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for defn in REGISTRY:
        stored = await app_settings.get_setting(defn.key, default="")
        effective = await get_effective(defn.key)
        env_val = _env_value(defn.env_attr) if defn.env_attr else ""
        display = effective
        if defn.secret and not reveal_secrets:
            display = _mask(effective)
        items.append({
            "key": defn.key,
            "group": defn.group,
            "label": defn.label,
            "valueType": defn.value_type,
            "secret": defn.secret,
            "editable": defn.editable,
            "restartRequired": defn.restart_required,
            "highUsage": defn.high_usage,
            "help": defn.help,
            "placeholder": defn.placeholder,
            "value": display if defn.secret else effective,
            "configured": bool(effective),
            "overridden": bool(stored),
            "envConfigured": bool(env_val),
            "hint": _mask(effective) if defn.secret else "",
        })
    return {"groups": GROUPS, "settings": items}


async def update_config(updates: dict[str, str | None]) -> dict[str, Any]:
    """Apply partial updates. None or missing = no change; \"\" clears DB override."""
    from app.services import scraper_settings as ss

    for key, raw in updates.items():
        defn = _BY_KEY.get(key)
        if not defn:
            raise ValueError(f"Unknown setting: {key}")
        if not defn.editable:
            raise ValueError(f"Setting is not editable: {key}")
        if raw is None:
            continue
        value = str(raw).strip() if not isinstance(raw, bool) else ("true" if raw else "false")

        if key == "integrations.mullvad_account_number":
            digits = "".join(c for c in value if c.isdigit())
            await app_settings.set_setting(key, digits)
            invalidate_cache(key)
            if digits:
                import asyncio
                from pathlib import Path
                from app.services import mullvad as mullvad_svc

                try:
                    priv, addr = await asyncio.to_thread(mullvad_svc.register_wireguard, digits)
                    await app_settings.set_setting("integrations.mullvad_wg_private_key", priv)
                    await app_settings.set_setting("integrations.mullvad_wg_addresses", addr)
                    env_path = Path("/app/data/mullvad.env")
                    env_path.parent.mkdir(parents=True, exist_ok=True)
                    mullvad_svc.write_gluetun_env(
                        str(env_path),
                        private_key=priv,
                        addresses=addr,
                        account=digits,
                    )
                except Exception as e:
                    logger.exception("Mullvad WireGuard registration failed")
                    raise ValueError(f"Mullvad WireGuard registration failed: {e}") from e
            continue

        if key in ("scraper.abb_rss_only", "scraper.knaben_rss_only"):
            bool_val = value.lower() in ("1", "true", "yes", "on")
            field = "abb_rss_only" if "abb" in key else "knaben_rss_only"
            await ss.update_scraper_config({field: bool_val})
            invalidate_cache(key)
            continue

        await app_settings.set_setting(key, value)
        invalidate_cache(key)

    await apply_runtime_overrides()
    return await list_config()


async def setup_status() -> dict[str, Any]:
    """First-run checklist for the instance setup wizard."""
    abs_url, abs_key, _ = await get_abs_connection()
    kav_url, kav_key, _ = await get_kavita_connection()
    prow_url, prow_key = await get_prowlarr_connection()
    rd = await get_effective("config.real_debrid_api_token")
    torbox = await get_effective("config.torbox_api_token")
    hc = await get_effective("integrations.hardcover_api_key")
    abb_rss = await get_effective_bool("scraper.abb_rss_only", True)
    knaben_rss = await get_effective_bool("scraper.knaben_rss_only", True)

    steps = [
        {
            "id": "libraries",
            "label": "Audiobookshelf / Kavita",
            "done": bool(abs_url and abs_key) or bool(kav_url and kav_key),
            "required": True,
            "help": "Connect at least one library (ABS for audiobooks, Kavita for ebooks).",
        },
        {
            "id": "indexers",
            "label": "Prowlarr",
            "done": bool(prow_url and prow_key),
            "required": True,
            "help": "Needed for torrent search and the indexer cache scraper.",
        },
        {
            "id": "debrid",
            "label": "Debrid (RD or TorBox)",
            "done": bool(rd or torbox),
            "required": False,
            "help": "Server default keys. Users can also set keys per library group.",
        },
        {
            "id": "catalog",
            "label": "Catalog APIs (optional)",
            "done": bool(hc),
            "required": False,
            "help": "Hardcover for ratings/series/curated shelves. NYT/ISBNdb optional.",
        },
        {
            "id": "scraper",
            "label": "Scraper mode",
            "done": True,  # always "done" once defaults applied
            "required": False,
            "help": "Defaults to RSS-only (safe on a Pi). Deep crawl is opt-in.",
            "abbRssOnly": abb_rss,
            "knabenRssOnly": knaben_rss,
        },
    ]
    required_done = all(s["done"] for s in steps if s["required"])
    return {
        "complete": required_done,
        "steps": steps,
        "defaults": {
            "abbRssOnly": True,
            "knabenRssOnly": True,
            "abbAuthorCrawl": False,
            "abbLiveSearch": False,
        },
    }


async def apply_setup_defaults() -> None:
    """Ensure RSS-only scraper defaults are persisted for fresh installs."""
    from app.services import scraper_settings as ss

    await ss.update_scraper_config({
        "abb_rss_only": True,
        "knaben_rss_only": True,
        "rss_every_n_jobs": 1,
    })
    invalidate_cache("scraper.abb_rss_only")
    invalidate_cache("scraper.knaben_rss_only")
    await app_settings.set_setting("config.abb_author_crawl_enabled", "false")
    await app_settings.set_setting("config.abb_live_search_enabled", "false")
    invalidate_cache("config.abb_author_crawl_enabled")
    invalidate_cache("config.abb_live_search_enabled")
    await apply_runtime_overrides()
    logger.info("Applied recommended RSS-only scraper defaults")


async def apply_runtime_overrides() -> None:
    """Push DB overrides onto the process-wide Settings singleton.

    Lets existing ``settings.foo`` call sites pick up Admin Config changes
    without rewriting every service. Env-only / non-editable fields are skipped.
    """
    import os
    from app.config import _host_for_docker

    s = get_settings()
    for defn in REGISTRY:
        if not defn.env_attr or not defn.editable:
            continue
        if defn.key.startswith("scraper."):
            continue  # scraper_settings owns these
        raw = await app_settings.get_setting(defn.key, default="")
        if not raw and defn.env_fallback:
            continue  # keep env default already on Settings
        if not raw:
            continue
        attr = defn.env_attr
        try:
            current = getattr(s, attr, None)
            if defn.value_type == "bool" or isinstance(current, bool):
                coerced: Any = raw.lower() in ("1", "true", "yes", "on")
            elif defn.value_type == "int" or isinstance(current, int):
                coerced = int(raw)
            elif defn.value_type == "float" or isinstance(current, float):
                coerced = float(raw)
            else:
                coerced = raw
                if attr in ("abs_url", "kavita_url") and os.path.exists("/.dockerenv"):
                    coerced = _host_for_docker(str(coerced))
            object.__setattr__(s, attr, coerced)
        except Exception as e:
            logger.debug("Runtime override %s failed: %s", attr, e)
    invalidate_cache()
