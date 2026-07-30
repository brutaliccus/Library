"""Unified instance configuration registry.

Editable settings live in ``app_settings`` (DB) with env-var fallbacks from
``Settings``. SECRET_KEY and VAPID private key stay env-only (read-only in UI).
Storage paths and staging dirnames are editable under Storage / Paths
(env fallback; container roots may need a restart to match compose mounts).
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
    SettingDef(
        key="config.invite_rotation_minutes",
        group="core",
        label="Invite code auto-rotation (minutes)",
        value_type="int",
        env_fallback=False,
        help=(
            "Automatically rotate each library invite code on this interval "
            "(60 minutes to 43200 = 30 days). Default 10080 = 7 days."
        ),
        placeholder="10080",
    ),
    SettingDef(
        key="config.android_apk_github_repo",
        group="mobile",
        label="Android APK GitHub repo",
        env_attr="android_apk_github_repo",
        help="owner/repo whose GitHub Releases host the Library APK (latest release with a .apk asset).",
        placeholder="brutaliccus/Library",
    ),
    SettingDef(
        key="config.github_token",
        group="mobile",
        label="GitHub token (optional)",
        env_attr="github_token",
        secret=True,
        help="Optional PAT for higher GitHub API rate limits when checking for APK updates.",
        placeholder="ghp_…",
    ),
    SettingDef(
        key="config.android_min_version_code",
        group="mobile",
        label="Minimum Android versionCode",
        env_attr="android_min_version_code",
        value_type="int",
        help=(
            "Installed APKs with a lower versionCode are blocked until they update "
            "(default 59 = Library 1.58). Raise this after publishing a required release."
        ),
        placeholder="56",
    ),
    SettingDef(
        key="config.android_force_updates",
        group="mobile",
        label="Force Android APK updates",
        env_attr="android_force_updates",
        value_type="bool",
        help=(
            "When on, a newer GitHub APK is a hard gate (blocking modal, no dismiss). "
            "When off, only installs below Minimum Android versionCode are forced."
        ),
    ),
    # --- Libraries ---
    SettingDef(
        key="config.abs_url",
        group="libraries",
        label="Audiobookshelf URL",
        env_attr="abs_url",
        help=(
            "Bundled-media profile: `http://audiobookshelf:80`. External: Windows → "
            "`http://host.docker.internal:13378`; Linux/Pi → `http://172.17.0.1:13378`."
        ),
        placeholder="http://audiobookshelf:80",
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
        help=(
            "Bundled-media profile: `http://kavita:5000`. External: Windows → "
            "`http://host.docker.internal:5000`; Linux/Pi → `http://172.17.0.1:5000`."
        ),
        placeholder="http://kavita:5000",
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
    # --- Pipelines (LibraForge + ebooks) ---
    SettingDef(
        key="config.libraforge_url",
        group="pipeline",
        label="LibraForge public URL",
        env_attr="libraforge_url",
        help=(
            "Browser / Admin deep-link. Bundled-media: `http://127.0.0.1:5056`. "
            "External sibling: see docs/libraforge.md."
        ),
        placeholder="http://127.0.0.1:5056",
    ),
    SettingDef(
        key="config.libraforge_internal_url",
        group="pipeline",
        label="LibraForge internal URL",
        env_attr="libraforge_internal_url",
        help=(
            "Reachable from the Library container (API + health). Bundled-media: "
            "`http://libraforge:5056`. External: Windows `host.docker.internal:5056` / "
            "Linux `172.17.0.1:5056`."
        ),
        placeholder="http://libraforge:5056",
    ),
    SettingDef(
        key="config.libraforge_pipeline_enabled",
        group="pipeline",
        label="Audiobook LibraForge pipeline",
        env_attr="libraforge_pipeline_enabled",
        value_type="bool",
        help=(
            "Land downloads in audiobook staging (default /audiobooks/.unorganized) → Metadata → M4B → "
            "Chapter Forge (ASIN) → Folder Forge → ABS. Cross-request M4B is serialized (concurrency 1). "
            "Staging name is under Config → Storage / Paths."
        ),
    ),
    SettingDef(
        key="config.libraforge_min_score",
        group="pipeline",
        label="LibraForge min score",
        env_attr="libraforge_min_score",
        value_type="float",
        help="Below this confidence → quarantine for admin Quick Review.",
        placeholder="0.70",
    ),
    SettingDef(
        key="config.libraforge_naming_template",
        group="pipeline",
        label="Folder Forge naming template",
        env_attr="libraforge_naming_template",
        help=(
            "Folder path for Folder Forge. Use the path builder on Library Sweep / Pipelines "
            "(tokens: author, series, edition, title, filename, narrator, year, asin, …)."
        ),
        placeholder="{author}/{series} [{edition}]/{title}/{filename}",
    ),
    SettingDef(
        key="config.libraforge_metadata_provider",
        group="pipeline",
        label="Default metadata provider",
        env_attr="libraforge_metadata_provider",
        help=(
            "Primary Metadata Forge source: audible, graphicaudio, or soundbooththeater. "
            "On a miss, LibraForge tries Graphic Audio then Soundbooth Theater. "
            "Also editable on the Library Sweep tab."
        ),
        placeholder="audible",
    ),
    SettingDef(
        key="config.library_sweep_abs_scan_every",
        group="pipeline",
        label="Library Sweep ABS scan every N books",
        env_attr="library_sweep_abs_scan_every",
        value_type="int",
        help=(
            "Full Audiobookshelf library scan after this many successfully completed "
            "Sweep books (and whenever Sweep completes / pauses / cancels / stops). "
            "Per-book scans are skipped during Sweep to keep runs fast."
        ),
        placeholder="25",
    ),
    SettingDef(
        key="config.libraforge_m4b_jobs",
        group="pipeline",
        label="LibraForge M4B jobs (per run)",
        env_attr="libraforge_m4b_jobs",
        value_type="int",
        help=(
            "ffmpeg/m4b-tool workers inside one encode (default 1 on Pi). "
            "Separate from Library Site’s global M4B queue, which always runs one encode at a time."
        ),
        placeholder="1",
    ),
    SettingDef(
        key="config.ebook_pipeline_enabled",
        group="pipeline",
        label="Ebook organizer pipeline",
        env_attr="ebook_pipeline_enabled",
        value_type="bool",
        help=(
            "Land in ebook staging (default /ebooks/unorganized) → identify → Author/Series/Title → Kavita. "
            "Staging name is under Config → Storage / Paths. See docs/ebooks.md."
        ),
    ),
    SettingDef(
        key="config.library_sweep_skip_m4b",
        group="pipeline",
        label="Library Sweep: skip M4B processing",
        value_type="bool",
        env_fallback=False,
        help=(
            "When off (default), Sweep runs M4B conversion when needed. "
            "When on, skip M4B entirely and keep the existing audio layout."
        ),
    ),
    SettingDef(
        key="config.library_sweep_force_metadata_forge",
        group="pipeline",
        label="Library Sweep: force metadata forging",
        value_type="bool",
        env_fallback=False,
        help=(
            "When off, skip Metadata Forge if applied markers already exist. "
            "When on, always re-run Metadata Forge."
        ),
    ),
    SettingDef(
        key="config.library_sweep_force_chapter_forge",
        group="pipeline",
        label="Library Sweep: force chapter forging",
        value_type="bool",
        env_fallback=False,
        help=(
            "When off, skip Chapter Forge if the .m4b already has chapter markers. "
            "When on, always re-embed Audible chapters."
        ),
    ),
    SettingDef(
        key="config.library_sweep_force_folder_forge",
        group="pipeline",
        label="Library Sweep: force folder forging",
        value_type="bool",
        env_fallback=False,
        help=(
            "When off, skip Folder Forge if staging audio is already hardlinked into the library. "
            "When on, always run Folder Forge."
        ),
    ),
    SettingDef(
        key="allow_user_audiobook_upload",
        group="pipeline",
        label="Allow user audiobook uploads (legacy global)",
        value_type="bool",
        env_fallback=False,
        help=(
            "Legacy global toggle. Prefer per-user “can upload books” on the Users tab. "
            "Admins always can upload. Kept for migration/compat."
        ),
    ),
    SettingDef(
        key="config.ebook_min_score",
        group="pipeline",
        label="Ebook min score",
        env_attr="ebook_min_score",
        value_type="float",
        help="Below this confidence → quarantine under the ebook staging folder.",
        placeholder="0.70",
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
        label="TorBox API token (server default, optional)",
        env_attr="torbox_api_token",
        secret=True,
        help="Optional second debrid. Unique cache wins; both/neither cached → user preferred.",
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
        key="integrations.openrouter_enabled",
        group="catalog",
        label="OpenRouter LLM assist",
        env_attr="openrouter_enabled",
        value_type="bool",
        help="LLM assist for Metadata Forge / ebook identify retry, multi-book split, "
             "file prune, and ASIN recovery. Off by default — no calls without a key.",
    ),
    SettingDef(
        key="integrations.openrouter_api_key",
        group="catalog",
        label="OpenRouter API key",
        env_attr="openrouter_api_key",
        secret=True,
        help="https://openrouter.ai/keys — chat completions + GET /api/v1/key usage.",
    ),
    SettingDef(
        key="integrations.openrouter_model",
        group="catalog",
        label="OpenRouter model",
        env_attr="openrouter_model",
        placeholder="openai/gpt-4o-mini",
        help="OpenRouter model id. Default openai/gpt-4o-mini (cheap + capable).",
    ),
    SettingDef(
        key="integrations.openrouter_confidence_threshold",
        group="catalog",
        label="OpenRouter confidence threshold",
        env_attr="openrouter_confidence_threshold",
        value_type="float",
        placeholder="0.85",
        help="Auto-apply LLM actions only when confidence ≥ this (0–1). Default 0.85.",
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
    # --- Storage / Paths ---
    SettingDef(
        key="config.audiobook_dir",
        group="storage",
        label="Audiobook directory (container)",
        env_attr="audiobook_dir",
        restart_required=True,
        help=(
            "In-container path for the audiobook library (default /audiobooks). "
            "Must match the docker-compose volume target. Host bind path is "
            "AUDIOBOOK_HOST_DIR in .env / compose — not editable here."
        ),
        placeholder="/audiobooks",
    ),
    SettingDef(
        key="config.ebook_dir",
        group="storage",
        label="Ebook directory (container)",
        env_attr="ebook_dir",
        restart_required=True,
        help=(
            "In-container path for the ebook library (default /ebooks). "
            "Must match the docker-compose volume target. Host bind path is "
            "EBOOK_HOST_DIR in .env / compose — not editable here."
        ),
        placeholder="/ebooks",
    ),
    SettingDef(
        key="config.audiobook_staging_dirname",
        group="storage",
        label="Audiobook staging folder name",
        env_attr="audiobook_staging_dirname",
        help=(
            "Folder under the audiobook directory where downloads land before LibraForge "
            "(default .unorganized). Prefer a dot-name so Audiobookshelf skips it."
        ),
        placeholder=".unorganized",
    ),
    SettingDef(
        key="config.audiobook_staging_legacy_dirname",
        group="storage",
        label="Audiobook staging legacy name",
        env_attr="audiobook_staging_legacy_dirname",
        help=(
            "Also treated as staging (migration / path remap). Default _unorganized. "
            "Keep both if you still have legacy folders on disk."
        ),
        placeholder="_unorganized",
    ),
    SettingDef(
        key="config.ebook_staging_dirname",
        group="storage",
        label="Ebook staging folder name",
        env_attr="ebook_staging_dirname",
        help=(
            "Folder under the ebook directory for pipeline staging (default unorganized). "
            "Configure Kavita to exclude this name from its library root."
        ),
        placeholder="unorganized",
    ),
    SettingDef(
        key="config.ol_catalog_db_path",
        group="storage",
        label="Open Library catalog DB",
        env_attr="ol_catalog_db_path",
        restart_required=True,
        help="SQLite path for the local Open Library catalog (usually on fast storage).",
        placeholder="/app/data/ol_catalog.db",
    ),
    SettingDef(
        key="config.ol_dumps_dir",
        group="storage",
        label="Open Library dumps directory",
        env_attr="ol_dumps_dir",
        restart_required=True,
        help="Where monthly Open Library dump files are downloaded/stored (often a large disk).",
        placeholder="/openlibrary/dumps",
    ),
]

GROUPS: list[dict[str, str]] = [
    {"id": "core", "label": "Core"},
    {"id": "mobile", "label": "Android / mobile"},
    {"id": "libraries", "label": "Libraries (ABS / Kavita)"},
    {"id": "pipeline", "label": "Pipelines (LibraForge / ebooks)"},
    {"id": "indexers", "label": "Indexers"},
    {"id": "debrid", "label": "Debrid (server defaults)"},
    {"id": "catalog", "label": "Catalog APIs"},
    {"id": "vpn", "label": "VPN / ABB proxy"},
    {"id": "notifications", "label": "Push notifications"},
    {"id": "scraper", "label": "Discovery flags"},
    {"id": "storage", "label": "Storage / Paths"},
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

        if key in (
            "config.audiobook_staging_dirname",
            "config.audiobook_staging_legacy_dirname",
            "config.ebook_staging_dirname",
        ):
            # Folder name only — no slashes / traversal (empty clears to env default).
            if value:
                if "/" in value or "\\" in value or value in (".", "..") or ".." in value:
                    raise ValueError(
                        f"{key} must be a single folder name (no slashes), got: {value!r}"
                    )

        await app_settings.set_setting(key, value)
        invalidate_cache(key)

    await apply_runtime_overrides()
    return await list_config()


# Docker / host URL presets for the instance setup wizard (editable in the UI).
SETUP_STACK_PRESETS: dict[str, dict[str, str]] = {
    "bundled_media": {
        "label": "Bundled media (compose profile)",
        "config.abs_url": "http://audiobookshelf:80",
        "config.kavita_url": "http://kavita:5000",
        "config.libraforge_url": "http://127.0.0.1:5056",
        "config.libraforge_internal_url": "http://libraforge:5056",
        "config.libraforge_pipeline_enabled": "true",
        "config.ebook_pipeline_enabled": "true",
        "config.libraforge_m4b_jobs": "1",
    },
    "windows_docker": {
        "label": "Windows (external / host.docker.internal)",
        "config.abs_url": "http://host.docker.internal:13378",
        "config.kavita_url": "http://host.docker.internal:5000",
        "config.libraforge_url": "http://127.0.0.1:5056",
        "config.libraforge_internal_url": "http://host.docker.internal:5056",
        "config.libraforge_pipeline_enabled": "true",
        "config.ebook_pipeline_enabled": "true",
        "config.libraforge_m4b_jobs": "1",
    },
    "linux_docker": {
        "label": "Linux / Pi (external bridge)",
        "config.abs_url": "http://172.17.0.1:13378",
        "config.kavita_url": "http://172.17.0.1:5000",
        "config.libraforge_url": "http://127.0.0.1:5056",
        "config.libraforge_internal_url": "http://172.17.0.1:5056",
        "config.libraforge_pipeline_enabled": "true",
        "config.ebook_pipeline_enabled": "true",
        "config.libraforge_m4b_jobs": "1",
    },
}


def _is_bundled_media_url(url: str | None) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    return any(
        host in u
        for host in (
            "://audiobookshelf",
            "://kavita:",
            "://kavita/",
            "://libraforge",
            "audiobookshelf:80",
            "kavita:5000",
            "libraforge:5056",
        )
    )


async def setup_status() -> dict[str, Any]:
    """First-run checklist for the instance setup wizard."""
    abs_url, abs_key, _ = await get_abs_connection()
    kav_url, kav_key, _ = await get_kavita_connection()
    prow_url, prow_key = await get_prowlarr_connection()
    rd = await get_effective("config.real_debrid_api_token")
    torbox = await get_effective("config.torbox_api_token")
    hc = await get_effective("integrations.hardcover_api_key")
    lf_url = await get_effective("config.libraforge_url")
    lf_internal = await get_effective("config.libraforge_internal_url")
    lf_on = await get_effective_bool("config.libraforge_pipeline_enabled", False)
    apk_repo = await get_effective("config.android_apk_github_repo")
    abb_rss = await get_effective_bool("scraper.abb_rss_only", True)
    knaben_rss = await get_effective_bool("scraper.knaben_rss_only", True)

    stack_done = bool(abs_url and abs_key) or bool(kav_url and kav_key)
    bundled_media = (
        _is_bundled_media_url(abs_url)
        or _is_bundled_media_url(kav_url)
        or _is_bundled_media_url(lf_internal)
    )
    bundled_ready = bundled_media and bool(abs_url and abs_key) and bool(kav_url and kav_key)

    # Soft probe: Audible auth lives in LibraForge's mounted /auth file, not Library .env.
    audible_configured = False
    audible_reachable = False
    audible_name = ""
    if lf_url or lf_internal:
        try:
            from app.services import libraforge as lf
            from app.services.libraforge import LibraForgeError

            status = await lf.auth_status()
            audible_reachable = True
            audible_configured = bool(status.get("auth_ok"))
            audible_name = str(status.get("active_name") or "")
        except LibraForgeError:
            audible_reachable = False
        except Exception:
            audible_reachable = False

    steps = [
        {
            "id": "stack",
            "label": "Library stack (ABS / Kavita / LibraForge)",
            "done": stack_done,
            "required": True,
            "help": (
                "Fresh installs use compose profile bundled-media: Audiobookshelf, Kavita, and "
                "LibraForge on the shared Docker network with API keys synced automatically. "
                "If keys are already present, continue without re-entering them. Advanced overrides "
                "still allow external ABS/Kavita/LF URLs (Pi production). Soft health probes run "
                "when you continue."
            ),
        },
        {
            "id": "audible",
            "label": "Audible account (metadata)",
            "done": audible_configured,
            "required": False,
            "help": (
                "Sign in once so Metadata Forge / Chapter Forge can look up Audible metadata and "
                "chapters. Credentials are stored only in LibraForge's auth mount "
                "(audible-metadata.json) - never in Library Site .env. Use a dedicated Audible "
                "account when possible. Re-auth later under Admin -> Integrations."
            ),
            "audibleConfigured": audible_configured,
            "audibleReachable": audible_reachable,
            "audibleName": audible_name,
        },
        {
            "id": "indexers",
            "label": "Prowlarr / Flare / Jackett",
            "done": bool(prow_url and prow_key),
            "required": True,
            "help": (
                "Prowlarr for torrent search + scraper. Jackett/FlareSolverr are compose sidecars "
                "(ABB RSS + live search). Deep Flare crawls stay opt-in on the Scraper step."
            ),
        },
        {
            "id": "debrid",
            "label": "Debrid (RD or TorBox)",
            "done": bool(rd or torbox),
            "required": False,
            "help": (
                "Server defaults; users can also set keys per library group. TorBox is optional. "
                "When both are configured: unique cache wins; both/neither → user preferred provider."
            ),
        },
        {
            "id": "folders",
            "label": "Staging folders & extras",
            "done": True,
            "required": False,
            "help": (
                "Confirm media staging + optional clients. Pipelines create staging roots under "
                "AUDIOBOOK_DIR / EBOOK_DIR automatically (defaults `/audiobooks/.unorganized` and "
                "`/ebooks/unorganized`). Override names in Admin → Config → Storage / Paths."
            ),
        },
        {
            "id": "openlibrary",
            "label": "Open Library catalog (optional)",
            "done": True,  # never blocks finishing onboarding
            "required": False,
            "help": (
                "Optional multi-GB local Open Library catalog. Skip freely — the shipped indexer "
                "cache seed is enough for release search. Or start a build now, or schedule it "
                "for off-peak (same controls as Admin → Catalog)."
            ),
        },
        {
            "id": "catalog",
            "label": "Catalog APIs (optional)",
            "done": bool(hc),
            "required": False,
            "help": (
                "Hardcover for store ratings/series/curated shelves (optional). "
                "My Library keeps ABS/local metadata authoritative; Hardcover genres fill empty only. "
                "NYT/ISBNdb optional."
            ),
        },
        {
            "id": "mobile",
            "label": "Android APK updates",
            "done": bool(apk_repo),
            "required": False,
            "help": (
                "GitHub `owner/repo` whose Releases host the Library APK "
                "(latest .apk asset). Force updates + minimum versionCode "
                "(default 54 = 1.53) gate old installs. See docs/android-app.md."
            ),
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
            "libraforgePipelineEnabled": True,
            "ebookPipelineEnabled": True,
        },
        "presets": SETUP_STACK_PRESETS,
        "stack": {
            "absConfigured": bool(abs_url and abs_key),
            "kavitaConfigured": bool(kav_url and kav_key),
            "libraforgeConfigured": bool(lf_url or lf_internal),
            "libraforgePipelineEnabled": lf_on,
            "bundledMedia": bundled_media,
            "bundledReady": bundled_ready,
        },
        "audible": {
            "configured": audible_configured,
            "reachable": audible_reachable,
            "activeName": audible_name,
        },
    }


async def validate_setup_connections() -> dict[str, Any]:
    """Soft health probes for the stack setup step (warnings only — never hard-fail)."""
    import asyncio

    from app.services.health_checks import (
        _probe_abs,
        _probe_kavita,
        _probe_libraforge,
        _probe_prowlarr,
    )

    abs_p, kav_p, lf_p, prow_p = await asyncio.gather(
        _probe_abs(),
        _probe_kavita(),
        _probe_libraforge(),
        _probe_prowlarr(),
    )
    probes = {
        "audiobookshelf": abs_p,
        "kavita": kav_p,
        "libraforge": lf_p,
        "prowlarr": prow_p,
    }
    abs_url, abs_key, _ = await get_abs_connection()
    kav_url, kav_key, _ = await get_kavita_connection()
    lf_internal = await get_effective("config.libraforge_internal_url")
    bundled_media = (
        _is_bundled_media_url(abs_url)
        or _is_bundled_media_url(kav_url)
        or _is_bundled_media_url(lf_internal)
    )
    bundled_ready = bundled_media and bool(abs_url and abs_key) and bool(kav_url and kav_key)

    warnings: list[str] = []
    for name, probe in probes.items():
        if not probe.get("configured"):
            continue
        if not probe.get("connected"):
            err = probe.get("error") or "unreachable"
            # Soft: bundled installs may still be warming; never hard-block.
            if bundled_ready and name in ("audiobookshelf", "kavita", "libraforge"):
                warnings.append(f"{name}: bundled stack still warming ({err})")
            else:
                warnings.append(f"{name}: configured but not reachable ({err})")
    if not (abs_p.get("configured") or kav_p.get("configured")):
        if not bundled_media:
            warnings.append(
                "Configure at least Audiobookshelf or Kavita (URL + API key), "
                "or enable compose profile bundled-media."
            )
    return {
        "ok": True,  # soft-fail: UI may continue with warnings
        "warnings": warnings,
        "probes": probes,
        "bundledMedia": bundled_media,
        "bundledReady": bundled_ready,
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


_OLD_SWEEP_ALLOW_M4B = "config.library_sweep_allow_m4b"
_SWEEP_SKIP_M4B = "config.library_sweep_skip_m4b"


async def migrate_library_sweep_m4b_setting() -> None:
    """Invert legacy allow_m4b → skip_m4b once, then drop the old key."""
    new_stored = await app_settings.get_setting(_SWEEP_SKIP_M4B, default="")
    old_stored = await app_settings.get_setting(_OLD_SWEEP_ALLOW_M4B, default="")
    if new_stored:
        if old_stored:
            await app_settings.set_setting(_OLD_SWEEP_ALLOW_M4B, "")
            invalidate_cache(_OLD_SWEEP_ALLOW_M4B)
        return
    if not old_stored:
        return
    allow = old_stored.strip().lower() in ("1", "true", "yes", "on")
    skip = not allow
    await app_settings.set_setting(_SWEEP_SKIP_M4B, "true" if skip else "false")
    await app_settings.set_setting(_OLD_SWEEP_ALLOW_M4B, "")
    invalidate_cache(_SWEEP_SKIP_M4B)
    invalidate_cache(_OLD_SWEEP_ALLOW_M4B)
    logger.info(
        "Migrated %s → %s (skip_m4b=%s)",
        _OLD_SWEEP_ALLOW_M4B,
        _SWEEP_SKIP_M4B,
        skip,
    )


async def apply_runtime_overrides() -> None:
    """Push DB overrides onto the process-wide Settings singleton.

    Lets existing ``settings.foo`` call sites pick up Admin Config changes
    without rewriting every service. Env-only / non-editable fields are skipped.
    """
    import os
    from app.config import _host_for_docker

    try:
        await migrate_library_sweep_m4b_setting()
    except Exception as e:
        logger.warning("library_sweep M4B setting migration skipped: %s", e)

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
                if attr in ("abs_url", "kavita_url", "libraforge_internal_url") and os.path.exists(
                    "/.dockerenv"
                ):
                    coerced = _host_for_docker(str(coerced))
            object.__setattr__(s, attr, coerced)
        except Exception as e:
            logger.debug("Runtime override %s failed: %s", attr, e)
    invalidate_cache()
