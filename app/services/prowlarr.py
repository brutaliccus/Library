import asyncio
import logging
import re
import time
import unicodedata
from typing import Any

import httpx
from app.config import get_settings
from app.services.real_debrid import extract_info_hash

logger = logging.getLogger(__name__)
settings = get_settings()

# Reject titles where non-Latin letters are at least this fraction of all letters.
# Accented Latin (French/Spanish/German) still counts as Latin and is kept.
FOREIGN_SCRIPT_RATIO = 0.5

# Strip release noise before measuring script ratio so `[m4b]` / `(2024)` / `1080p`
# can't dilute a CJK/Cyrillic title under the threshold.
_FOREIGN_NOISE_RE = re.compile(
    r"\[.*?\]|\(.*?\)|\{.*?\}|"
    r"\b(19|20)\d{2}\b|"
    r"\.(m4b|m4a|epub|pdf|mobi|mp3|azw3|flac|aac|ogg|iso)\b|"
    r"\b(audiobook|ebook|unabridged|abridged|m4b|epub|pdf|mp3|flac|"
    r"1080p|720p|480p|2160p|4k|bluray|web-dl)\b",
    re.IGNORECASE,
)


def _is_latin_letter(ch: str) -> bool:
    """True for Latin-script letters (including diacritics); False for other scripts."""
    if not ch.isalpha():
        return False
    # UNICODE name is "LATIN …" for basic + extended Latin. Fallback for any letter
    # whose category is Letter but name lookup fails: treat as non-Latin (safer).
    name = unicodedata.name(ch, "")
    return name.startswith("LATIN ")


def title_is_mostly_foreign_script(
    title: str, *, threshold: float = FOREIGN_SCRIPT_RATIO,
) -> bool:
    """True when ≥ ``threshold`` of the title's letters are non-Latin script.

    Digits, punctuation, whitespace, and common release tags (``.m4b``, ``[2024]``,
    ``audiobook``) are ignored so they don't dilute CJK/Cyrillic/Hangul titles.
    Empty / letter-less titles return False (not foreign — just uninformative).
    """
    cleaned = _FOREIGN_NOISE_RE.sub(" ", title or "")
    letters = [c for c in cleaned if c.isalpha()]
    if not letters:
        return False
    foreign = sum(1 for c in letters if not _is_latin_letter(c))
    return (foreign / len(letters)) >= threshold

# Empty = query all enabled indexers; Prowlarr still returns mixed results and we filter client-side.
# Forcing Torznab categories (3030/7020) on this host made some searches hang on FlareSolverr indexers.
DEFAULT_BOOK_CATEGORIES: list[int] = []

_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 300  # 5 minutes

_abb_indexer_id: int | None = None
_abb_indexer_cached_at: float = 0.0
_ABB_INDEXER_TTL = 3600

AUDIOBOOK_KEYWORDS = re.compile(
    r"audiobook|m4b|unabridged|abridged|narrated\s+by|full[- ]cast|read\s+by",
    re.IGNORECASE,
)
EBOOK_KEYWORDS = re.compile(
    r"\.epub\b|\.mobi\b|\.azw\b|\.cbr\b|\.cbz\b|\.pdf\b"
    r"|\bebook\b|\be[\-\s]?book\b|\bkindle\b|\bcalibre\b|\bepub\b|\bmobi\b|\bpdf\b",
    re.IGNORECASE,
)
AUDIOBOOK_CATS = {3030}  # Audio/Audiobook
EBOOK_CATS = {7020}  # Books/EBook
BOOK_RELATED_RANGES = {3, 7}  # Audio (3xxx), Books (7xxx)

SIZE_AUDIOBOOK_MIN = 100 * 1024 * 1024  # 100 MB
SIZE_EBOOK_MAX = 50 * 1024 * 1024  # 50 MB


def _standard_cat_ids(categories: list[dict]) -> set[int]:
    ids = set()
    for c in categories:
        if not isinstance(c, dict):
            continue
        cat_id = c.get("id")
        if cat_id and cat_id < 100000:
            ids.add(cat_id)
        for sc in c.get("subCategories", []):
            sc_id = sc.get("id")
            if sc_id and sc_id < 100000:
                ids.add(sc_id)
    return ids


def _indexer_name_parts(indexer: str) -> tuple[str, str]:
    name = (indexer or "").lower()
    return name, name.replace(" ", "")


def _is_audiobookbay_indexer(indexer: str) -> bool:
    n, compact = _indexer_name_parts(indexer)
    if "audiobookbay" in compact:
        return True
    if "audiobook" in n and "bay" in n:
        return True
    # Some Jackett/Prowlarr setups use spaced or hyphenated variants
    normalized = n.replace("-", " ").replace("_", " ")
    if "audio book bay" in normalized or "audiobooks bay" in normalized:
        return True
    return False


def _is_knaben_indexer(indexer: str) -> bool:
    n, compact = _indexer_name_parts(indexer)
    return "knaben" in n or "knaben" in compact


# Knaben is a general torrent indexer — reject obvious non-book results.
_NON_BOOK_TITLE = re.compile(
    r"\.(mp4|mkv|avi|wmv|flv|webm|ts|m2ts|exe|msi|iso)\b"
    r"|\b(?:1080p|720p|480p|2160p|4k|x264|x265|hevc|bluray|web-dl|webrip|mpeg2)\b"
    r"|\b\d{3,4}x\d{3,4}\b"
    r"|\b(?:season|s\d{2}e\d{2}|complete\s+series|tv\s+mini\s+series|mini\s+series)\b"
    r"|\b(?:pre-?activated|crack|keygen|ftuapps)\b"
    r"|\b(?:onlyfans|brazzers|bellesa|pornhub|xvideos|xxx)\b",
    re.IGNORECASE,
)


def _is_builtin_trusted_indexer(indexer: str) -> bool:
    return _is_audiobookbay_indexer(indexer) or _is_knaben_indexer(indexer)


def is_book_related(
    categories: list[dict],
    title: str = "",
    indexer: str = "",
    *,
    media_type: str | None = None,
    size_bytes: int = 0,
) -> bool:
    """Return True if the result is potentially a book (audiobook or ebook)."""
    if _is_audiobookbay_indexer(indexer):
        return True
    if _NON_BOOK_TITLE.search(title):
        return False
    if AUDIOBOOK_KEYWORDS.search(title) or EBOOK_KEYWORDS.search(title):
        return True
    if media_type in ("audiobook", "ebook"):
        return True
    if _is_knaben_indexer(indexer):
        cat_ids = _standard_cat_ids(categories)
        # Torznab-mapped audiobook/ebook only — not generic audio (3010 MP3, 3040 lossless).
        if cat_ids & (AUDIOBOOK_CATS | EBOOK_CATS):
            return True
        if cat_ids and any((cid // 1000) in BOOK_RELATED_RANGES for cid in cat_ids):
            return True
        if size_bytes > SIZE_AUDIOBOOK_MIN:
            return True
        return False
    for c in categories:
        name = (c.get("name") or "").lower()
        if any(k in name for k in ("audio", "book", "audiobook", "ebook", "novel")):
            return True
    cat_ids = _standard_cat_ids(categories)
    if not cat_ids:
        return True
    return any((cid // 1000) in BOOK_RELATED_RANGES for cid in cat_ids)


def detect_media_type(title: str, categories: list[dict], size_bytes: int = 0) -> str:
    cat_ids = _standard_cat_ids(categories)

    has_audio_kw = bool(AUDIOBOOK_KEYWORDS.search(title))
    has_ebook_kw = bool(EBOOK_KEYWORDS.search(title))

    # Title keywords are the strongest signal and always take priority
    if has_audio_kw and has_ebook_kw:
        # Both present (rare): let size break the tie
        return "audiobook" if size_bytes > SIZE_AUDIOBOOK_MIN else "ebook"
    if has_audio_kw:
        return "audiobook"
    if has_ebook_kw:
        return "ebook"

    # Fall back to Prowlarr categories
    in_audiobook_cat = bool(cat_ids & AUDIOBOOK_CATS)
    in_ebook_cat = bool(cat_ids & EBOOK_CATS)

    if in_audiobook_cat and not in_ebook_cat:
        # Category says audiobook, but small files are suspicious
        if 0 < size_bytes < SIZE_EBOOK_MAX:
            return "ebook"
        return "audiobook"
    if in_ebook_cat and not in_audiobook_cat:
        if size_bytes > SIZE_AUDIOBOOK_MIN:
            return "audiobook"
        return "ebook"

    # No keywords, no definitive category: use size heuristic
    if size_bytes > SIZE_AUDIOBOOK_MIN:
        return "audiobook"
    if 0 < size_bytes < SIZE_EBOOK_MAX:
        return "ebook"

    return "unknown"


def _cache_key(query: str, categories: list[int], indexer_ids: list[int] | None = None) -> str:
    idx = ",".join(map(str, sorted(indexer_ids or [])))
    return f"{query.lower().strip()}|{','.join(map(str, sorted(categories)))}|{idx}"


async def get_audiobookbay_indexer_id() -> int | None:
    """Prowlarr indexer id for AudioBook Bay (usually via Jackett)."""
    global _abb_indexer_id, _abb_indexer_cached_at
    now = time.time()
    if _abb_indexer_id is not None and now - _abb_indexer_cached_at < _ABB_INDEXER_TTL:
        return _abb_indexer_id

    if not settings.prowlarr_api_key:
        return None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.prowlarr_url}/api/v1/indexer",
                headers={"X-Api-Key": settings.prowlarr_api_key},
                timeout=15,
            )
            resp.raise_for_status()
            indexers = resp.json()
    except Exception as e:
        logger.warning("Could not list Prowlarr indexers: %s", e)
        return None

    found: int | None = None
    for idx in indexers:
        if not idx.get("enable"):
            continue
        name = idx.get("name") or ""
        if _is_audiobookbay_indexer(name):
            found = int(idx["id"])
            break

    if not found:
        names_cfg = (settings.prowlarr_trusted_indexer_names or "").strip()
        if names_cfg:
            wanted = [n.strip().lower() for n in names_cfg.split(",") if n.strip()]
            for idx in indexers:
                if not idx.get("enable"):
                    continue
                name = (idx.get("name") or "").lower()
                if any(w in name for w in wanted):
                    found = int(idx["id"])
                    logger.info("AudioBook Bay matched via PROWLARR_TRUSTED_INDEXER_NAMES: %s", idx.get("name"))
                    break

    _abb_indexer_id = found
    _abb_indexer_cached_at = now
    if found:
        logger.info("AudioBook Bay Prowlarr indexer id=%s", found)
    else:
        logger.warning("No enabled AudioBook Bay indexer in Prowlarr")
    return found


async def _list_enabled_indexers() -> list[dict]:
    if not settings.prowlarr_api_key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.prowlarr_url}/api/v1/indexer",
                headers={"X-Api-Key": settings.prowlarr_api_key},
                timeout=15,
            )
            resp.raise_for_status()
            return [i for i in resp.json() if i.get("enable")]
    except Exception as e:
        logger.warning("Could not list Prowlarr indexers: %s", e)
        return []


async def get_trusted_indexer_ids() -> list[int]:
    """AudioBook Bay, Knaben, optional extras — or all enabled indexers if none matched."""
    enabled = await _list_enabled_indexers()
    ids: list[int] = []
    seen: set[int] = set()

    for idx in enabled:
        name = idx.get("name") or ""
        if _is_builtin_trusted_indexer(name):
            iid = int(idx["id"])
            if iid not in seen:
                seen.add(iid)
                ids.append(iid)

    names_cfg = (settings.prowlarr_trusted_indexer_names or "").strip()
    if names_cfg:
        wanted = [n.strip().lower() for n in names_cfg.split(",") if n.strip()]
        for idx in enabled:
            iid = int(idx["id"])
            if iid in seen:
                continue
            name = (idx.get("name") or "").lower()
            if any(w in name for w in wanted):
                seen.add(iid)
                ids.append(iid)

    if not ids and enabled:
        ids = [int(i["id"]) for i in enabled]
        logger.info(
            "No ABB/Knaben match by name — using all %s enabled Prowlarr indexer(s)",
            len(ids),
        )
    elif ids:
        names = [i.get("name") for i in enabled if int(i["id"]) in ids]
        logger.info("Book search indexers: %s", ", ".join(names))
    return ids


async def get_knaben_indexer_ids() -> list[int]:
    enabled = await _list_enabled_indexers()
    return [int(i["id"]) for i in enabled if _is_knaben_indexer(i.get("name") or "")]


async def get_trusted_indexer_info() -> list[dict]:
    """Enabled trusted indexers as seen by Prowlarr (for admin diagnostics)."""
    enabled = await _list_enabled_indexers()
    out: list[dict] = []
    for idx in enabled:
        name = idx.get("name") or ""
        kind = "other"
        if _is_audiobookbay_indexer(name):
            kind = "audiobookbay"
        elif _is_knaben_indexer(name):
            kind = "knaben"
        out.append({"id": int(idx["id"]), "name": name, "kind": kind})
    return out


async def search_scraper_indexers(
    query: str,
    abb_limit: int | None = None,
    knaben_limit: int | None = None,
    timeout: int | None = None,
    *,
    skip_abb: bool = False,
) -> tuple[list[dict], dict[str, int]]:
    """Search ABB and Knaben separately so Knaben cannot crowd out ABB in the shared limit.

    ``skip_abb`` is set by background RSS-only mode so keyword crawl doesn't hit
    ABB (live download search still calls ``search_audiobookbay_multi`` directly).
    """
    import asyncio
    from app.services import audiobookbay
    from app.services.download_discovery import merge_indexer_results

    abb_limit = max(25, int(abb_limit if abb_limit is not None else settings.prowlarr_abb_search_limit))
    knaben_limit = max(25, int(knaben_limit if knaben_limit is not None else settings.prowlarr_search_limit))
    timeout = max(15, int(timeout if timeout is not None else settings.scraper_prowlarr_timeout))

    async def _abb() -> list[dict]:
        if skip_abb:
            return []
        # Default: Jackett ABB (~2 listing pages). Deep scrape only when explicitly enabled.
        if settings.abb_deep_search_enabled:
            try:
                pages = max(1, min(3, int(getattr(settings, "abb_scraper_max_pages", 2) or 2)))
                deep = await audiobookbay.search_deep(
                    query, max_pages=pages, resolve_hashes=False
                )
                if deep:
                    return deep[:abb_limit]
            except Exception as e:
                logger.warning("Scraper ABB deep search failed for %r: %s", query, e)
        rows = await search_audiobookbay_multi([query])
        return rows[:abb_limit]

    async def _knaben() -> list[dict]:
        if not await get_knaben_indexer_ids():
            return []
        rows = await search_knaben_multi([query], limit=knaben_limit, timeout=timeout)
        return rows[:knaben_limit]

    abb_res, knab_res = await asyncio.gather(_abb(), _knaben(), return_exceptions=True)

    abb_list: list[dict] = []
    knab_list: list[dict] = []
    if isinstance(abb_res, Exception):
        logger.warning("Scraper ABB search failed for %r: %s", query, abb_res)
    else:
        abb_list = abb_res
    if isinstance(knab_res, Exception):
        logger.warning("Scraper Knaben search failed for %r: %s", query, knab_res)
    else:
        knab_list = knab_res

    counts = {"abb": len(abb_list), "knaben": len(knab_list)}
    merged = merge_indexer_results(abb_list, knab_list)
    logger.info(
        "Scraper indexer search %r: ABB=%s Knaben=%s → %s merged",
        query, counts["abb"], counts["knaben"], len(merged),
    )
    return merged, counts


_TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"


def _parse_torznab_feed(xml_text: str, indexer_name: str) -> list[dict[str, Any]]:
    """Parse a Torznab RSS feed into the same result shape as search()."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Bad Torznab XML from %s: %s", indexer_name, e)
        return []

    results: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        attrs: dict[str, str] = {}
        for a in item.findall(f"{_TORZNAB_NS}attr"):
            name = a.get("name") or ""
            if name:
                attrs.setdefault(name.lower(), a.get("value") or "")

        size = 0
        size_text = item.findtext("size") or ""
        enclosure = item.find("enclosure")
        if size_text.isdigit():
            size = int(size_text)
        elif enclosure is not None and (enclosure.get("length") or "").isdigit():
            size = int(enclosure.get("length") or 0)

        cat_ids = [
            int(a.get("value") or 0)
            for a in item.findall(f"{_TORZNAB_NS}attr")
            if (a.get("name") or "").lower() == "category" and (a.get("value") or "").isdigit()
        ]
        raw_cats = [{"id": cid, "name": ""} for cid in cat_ids]

        media_type = detect_media_type(title, raw_cats, size)
        if (
            _is_audiobookbay_indexer(indexer_name)
            and media_type == "unknown"
            and size > SIZE_AUDIOBOOK_MIN
        ):
            media_type = "audiobook"

        if not is_book_related(
            raw_cats, title=title, indexer=indexer_name, media_type=media_type, size_bytes=size,
        ):
            continue

        download_url = item.findtext("link") or (
            enclosure.get("url") if enclosure is not None else ""
        ) or ""
        guid = item.findtext("guid") or ""
        magnet = attrs.get("magneturl") or ""
        info_hash = attrs.get("infohash") or ""
        if not magnet:
            if guid.startswith("magnet:"):
                magnet = guid
            elif download_url.startswith("magnet:"):
                magnet = download_url
            elif info_hash:
                magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={title}"
        if not info_hash and magnet:
            parsed = extract_info_hash(magnet, None, download_url)
            if parsed:
                info_hash = parsed

        def _int_attr(key: str) -> int:
            v = attrs.get(key) or ""
            return int(v) if v.isdigit() else 0

        results.append({
            "title": title,
            "size": size,
            "seeders": _int_attr("seeders"),
            "leechers": max(0, _int_attr("peers") - _int_attr("seeders")),
            "indexer": indexer_name,
            "publishDate": item.findtext("pubDate"),
            "magnetUrl": magnet or None,
            "downloadUrl": download_url or None,
            "guid": guid or None,
            "infoHash": info_hash.lower() if info_hash else "",
            "infoUrl": item.findtext("comments"),
            "categories": raw_cats,
            "mediaType": media_type,
        })
    return results


async def fetch_recent_releases(
    indexer_id: int,
    indexer_name: str,
    limit: int = 100,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Latest releases from one indexer via Prowlarr's Torznab feed (empty query).

    Much cheaper than keyword searches for keeping the cache stocked with
    genuinely new content — this is the same feed Sonarr/Radarr poll for RSS sync.
    """
    if not settings.prowlarr_api_key:
        return []
    url = f"{settings.prowlarr_url}/api/v1/indexer/{indexer_id}/newznab"
    params = {
        "t": "search",
        "q": "",
        "limit": str(limit),
        "apikey": settings.prowlarr_api_key,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
    results = _parse_torznab_feed(resp.text, indexer_name)
    logger.info("Torznab recent feed %s: %s book-related results", indexer_name, len(results))
    return results


async def fetch_recent_scraper_releases(
    limit_per_indexer: int = 100,
    timeout: int = 60,
    *,
    include_abb_flare: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Recent-release feeds from trusted scraper indexers, merged by hash.

    Background scraper should keep ``include_abb_flare=False`` — ABB via
    FlareSolverr/Chromium is reserved for occasional live book searches.
    When ``include_abb_flare`` is True and Mullvad ``abb_proxy_url`` is set,
    ABB recent posts are fetched via Flare+VPN.
    """
    import asyncio
    from app.services.download_discovery import merge_indexer_results

    indexers = await get_trusted_indexer_info()
    proxy = (getattr(settings, "abb_proxy_url", "") or "").strip()
    # Never use Jackett→ABB from the home IP for recent feeds.
    indexers = [i for i in indexers if not _is_audiobookbay_indexer(i.get("name") or "")]

    counts: dict[str, int] = {}
    batches: list[list[dict[str, Any]]] = []

    if include_abb_flare and proxy:
        from app.services import audiobookbay

        try:
            abb_rows = await audiobookbay.fetch_recent_listings(max_pages=1)
            abb_rows = await enrich_audiobookbay_for_cache(abb_rows[:limit_per_indexer])
            batches.append(abb_rows)
            counts["AudioBookBay(VPN)"] = len(abb_rows)
        except Exception as e:
            logger.warning("ABB VPN recent feed failed: %s", e)
            counts["AudioBookBay(VPN)"] = 0
    elif proxy:
        counts["AudioBookBay(VPN)"] = 0  # skipped — Flare reserved for live search

    if indexers:
        async def _one(idx: dict) -> list[dict[str, Any]]:
            return await fetch_recent_releases(
                idx["id"], idx["name"], limit=limit_per_indexer, timeout=timeout
            )

        gathered = await asyncio.gather(*[_one(i) for i in indexers], return_exceptions=True)
        for idx, batch in zip(indexers, gathered):
            if isinstance(batch, Exception):
                logger.warning("Recent feed failed for %s: %s", idx["name"], batch)
                counts[idx["name"]] = 0
                continue
            batches.append(batch)
            counts[idx["name"]] = len(batch)

    merged = merge_indexer_results(*batches) if batches else []
    if not proxy:
        abb = [r for r in merged if _is_audiobookbay_indexer(r.get("indexer") or "")]
        other = [r for r in merged if r not in abb]
        if abb:
            abb = await enrich_audiobookbay_for_cache(abb)
            merged = merge_indexer_results(abb, other)
    return merged, counts


async def enrich_audiobookbay_for_cache(
    rows: list[dict[str, Any]],
    *,
    limit: int | None = None,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Resolve ABB info hashes so Jackett Torznab rows can be upserted."""
    if not rows:
        return rows
    if all((r.get("infoHash") or "").strip() for r in rows):
        return rows
    from app.services.audiobookbay import resolve_hashes_for_results

    cap = max(
        0,
        int(
            limit
            if limit is not None
            else min(
                settings.abb_resolve_hash_limit,
                getattr(settings, "abb_scraper_resolve_hash_limit", 12) or 12,
            )
        ),
    )
    if cap <= 0:
        return rows
    try:
        coro = resolve_hashes_for_results(rows, limit=cap)
        if timeout is not None and timeout > 0:
            return await asyncio.wait_for(coro, timeout=timeout)
        return await coro
    except Exception as e:
        logger.warning("ABB hash enrich for cache failed: %s", e or type(e).__name__)
        return rows


async def search_audiobookbay_multi(
    queries: list[str],
    *,
    jackett_timeout: int | None = None,
    prowlarr_timeout: int | None = None,
    allow_flare_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Search ABB for live download UI.

    Jackett Torznab first (~1–3s). Direct Mullvad Flare multi-page scrape is a
    fallback only — it commonly takes 30–90s and made live search feel hung.
    """
    if not queries:
        return []
    query = queries[0]
    limit = max(25, int(settings.prowlarr_abb_search_limit))
    # Live UI must fail fast; do not inherit the 180s Jackett/Flare deploy budget.
    jt = max(10, int(jackett_timeout if jackett_timeout is not None else 25))

    jackett_rows = await search_jackett_audiobookbay(query, limit=limit, timeout=jt)
    if jackett_rows:
        logger.info("ABB Jackett direct: %s results for %r", len(jackett_rows), query[:60])
        # Torznab magnets already carry infoHash — skip Flare detail crawls here.
        return await enrich_audiobookbay_for_cache(jackett_rows, limit=0)

    proxy = (getattr(settings, "abb_proxy_url", "") or "").strip()
    if allow_flare_fallback and proxy:
        from app.services import audiobookbay

        try:
            deep = await asyncio.wait_for(
                audiobookbay.search_deep(
                    query, max_pages=2, resolve_hashes=False, for_live=True,
                ),
                timeout=35.0,
            )
            if deep:
                logger.info(
                    "ABB via Mullvad Flare (fallback): %s results for %r",
                    len(deep[:limit]),
                    query[:60],
                )
                return await enrich_audiobookbay_for_cache(deep[:limit], timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("ABB Mullvad Flare fallback timed out after 35s for %r", query[:60])
        except Exception as e:
            logger.warning("ABB Mullvad Flare fallback failed for %r: %s", query[:60], e)

    iid = await get_audiobookbay_indexer_id()
    if not iid:
        return []
    try:
        return await search(
            query,
            indexer_ids=[iid],
            limit=limit,
            timeout=prowlarr_timeout if prowlarr_timeout is not None else 30,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            logger.warning("Prowlarr ABB indexer unavailable — fix Jackett/FlareSolverr or re-enable in Prowlarr")
            return []
        raise


async def search_jackett_audiobookbay(
    query: str, limit: int = 150, timeout: int | None = None
) -> list[dict[str, Any]]:
    """Bypass Prowlarr when its ABB indexer is marked unavailable."""
    base = (settings.jackett_url or "").rstrip("/")
    key = (settings.jackett_api_key or "").strip()
    if not base or not key:
        return []

    url = f"{base}/api/v2.0/indexers/audiobookbay/results/torznab/api"
    read_timeout = max(
        15,
        int(
            timeout
            if timeout is not None
            else getattr(settings, "jackett_abb_timeout", 180) or 180
        ),
    )
    http_timeout = httpx.Timeout(read_timeout, connect=12.0)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"apikey": key, "t": "search", "q": query, "limit": str(limit)},
                timeout=http_timeout,
            )
            if resp.status_code >= 400:
                logger.warning("Jackett ABB HTTP %s: %s", resp.status_code, resp.text[:200])
                return []
        return _parse_torznab_feed(resp.text, "AudioBookBay")
    except httpx.TimeoutException:
        logger.warning("Jackett ABB search timed out after %ss for %r", read_timeout, query[:60])
        return []
    except Exception as e:
        logger.warning("Jackett ABB search failed: %s", e)
        return []


async def search_knaben_multi(
    queries: list[str],
    *,
    limit: int | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Search Knaben via direct API (includes unseeded audiobooks Prowlarr drops)."""
    from app.services import knaben

    if not queries:
        return []
    ids = await get_knaben_indexer_ids()
    if not ids:
        return []
    effective_limit = max(50, int(limit if limit is not None else settings.prowlarr_search_limit))
    return await knaben.search_multi(queries, limit=effective_limit, timeout=timeout)


async def search_trusted_indexers_multi(
    queries: list[str],
    *,
    knaben_queries: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search ABB via Jackett (~2 pages) + Knaben, then merge.

    When abb_deep_search_enabled is on, ABB uses direct multi-page scrape instead.
    """
    import asyncio
    from app.services import audiobookbay
    from app.services.download_discovery import merge_indexer_results

    if not queries:
        return []

    async def _abb() -> list[dict[str, Any]]:
        # Non-progressive path (smart-stream / fallback): one primary query, serial pages.
        if settings.abb_deep_search_enabled:
            try:
                deep = await audiobookbay.search_deep_multi(queries, resolve_hashes=False)
                if deep:
                    return deep
                logger.warning("ABB deep search empty — falling back to Prowlarr/Jackett")
            except Exception as e:
                logger.warning("ABB deep search failed, falling back to Prowlarr: %s", e)
        return await search_audiobookbay_multi(queries[:1] if queries else [])

    knab_q = knaben_queries if knaben_queries else queries

    async def _knaben() -> list[dict[str, Any]]:
        return await search_knaben_multi(knab_q)

    abb_res, knab_res = await asyncio.gather(_abb(), _knaben(), return_exceptions=True)
    abb_list: list[dict[str, Any]] = []
    knab_list: list[dict[str, Any]] = []
    if isinstance(abb_res, Exception):
        logger.warning("Trusted ABB search failed: %s", abb_res)
    else:
        abb_list = abb_res
    if isinstance(knab_res, Exception):
        logger.warning("Trusted Knaben search failed: %s", knab_res)
    else:
        knab_list = knab_res

    # Also include any other trusted indexers (comma-list in settings) via shared call.
    other_ids = []
    try:
        trusted = await get_trusted_indexer_info()
        abb_id = await get_audiobookbay_indexer_id()
        knaben_ids = set(await get_knaben_indexer_ids())
        for idx in trusted:
            iid = int(idx["id"])
            if idx.get("kind") == "other" and iid != abb_id and iid not in knaben_ids:
                other_ids.append(iid)
    except Exception:
        other_ids = []

    other_list: list[dict[str, Any]] = []
    if other_ids:
        try:
            other_list = await search_multi(
                queries,
                indexer_ids=other_ids,
                limit=max(50, int(settings.prowlarr_search_limit)),
            )
        except Exception as e:
            logger.warning("Other trusted indexer search failed: %s", e)

    merged = merge_indexer_results(abb_list, knab_list, other_list)
    logger.info(
        "Trusted multi-search: ABB=%s Knaben=%s other=%s → %s merged",
        len(abb_list),
        len(knab_list),
        len(other_list),
        len(merged),
    )
    return merged


async def search_multi(
    queries: list[str],
    categories: list[int] | None = None,
    indexer_ids: list[int] | None = None,
    limit: int | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Run several Prowlarr searches and merge by info hash (best seeders kept)."""
    import asyncio

    if not queries:
        return []
    if len(queries) == 1:
        return await search(
            queries[0],
            categories=categories,
            indexer_ids=indexer_ids,
            limit=limit,
            timeout=timeout,
        )

    gathered = await asyncio.gather(
        *[
            search(q, categories=categories, indexer_ids=indexer_ids, limit=limit, timeout=timeout)
            for q in queries
        ],
        return_exceptions=True,
    )
    merged: dict[str, dict[str, Any]] = {}
    for i, batch in enumerate(gathered):
        if isinstance(batch, Exception):
            logger.warning("Prowlarr search query %r failed: %s", queries[i], batch)
            continue
        for r in batch:
            key = (r.get("infoHash") or "").lower()
            if not key:
                key = r.get("magnetUrl") or r.get("downloadUrl") or f"{r.get('title')}|{r.get('indexer')}"
            prev = merged.get(key)
            if not prev or (r.get("seeders") or 0) > (prev.get("seeders") or 0):
                merged[key] = r
    out = list(merged.values())
    logger.info(
        "Prowlarr multi-search: %s queries → %s unique results",
        len(queries),
        len(out),
    )
    return out


async def search(
    query: str,
    categories: list[int] | None = None,
    indexer_ids: list[int] | None = None,
    limit: int | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    if categories is None:
        categories = DEFAULT_BOOK_CATEGORIES
    if limit is None:
        limit = max(50, int(settings.prowlarr_search_limit))

    key = _cache_key(query, categories, indexer_ids)
    now = time.time()
    if key in _cache:
        cached_at, results = _cache[key]
        if now - cached_at < CACHE_TTL:
            return results

    params: list[tuple[str, str]] = [
        ("query", query),
        ("apikey", settings.prowlarr_api_key),
        ("limit", str(limit)),
    ]
    for cat in categories:
        params.append(("categories", str(cat)))
    if indexer_ids:
        for iid in indexer_ids:
            params.append(("indexerIds", str(iid)))

    timeout = max(15, int(timeout or settings.prowlarr_search_timeout))
    t0 = time.perf_counter()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.prowlarr_url}/api/v1/search",
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        raw_results = resp.json()
    elapsed = time.perf_counter() - t0
    results = []
    skipped_category = 0
    for item in raw_results:
        raw_cats = item.get("categories", [])
        item_title = item.get("title", "") or ""
        item_indexer = item.get("indexer", "") or ""

        size = item.get("size", 0)
        media_type = detect_media_type(item_title, raw_cats, size)
        if (
            _is_audiobookbay_indexer(item_indexer)
            and media_type == "unknown"
            and size > SIZE_AUDIOBOOK_MIN
        ):
            media_type = "audiobook"

        if not is_book_related(
            raw_cats,
            title=item_title,
            indexer=item_indexer,
            media_type=media_type,
            size_bytes=size,
        ):
            skipped_category += 1
            continue

        magnet = None
        download_url = item.get("downloadUrl") or ""
        guid = item.get("guid", "") or ""
        info_hash = item.get("infoHash") or ""
        title_for_magnet = item_title

        if guid.startswith("magnet:"):
            magnet = guid
        elif download_url.startswith("magnet:"):
            magnet = download_url
        elif info_hash:
            magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={title_for_magnet}"

        if not info_hash and magnet:
            parsed = extract_info_hash(magnet, None, download_url)
            if parsed:
                info_hash = parsed

        results.append({
            "title": item_title or "Unknown",
            "size": size,
            "seeders": item.get("seeders", 0),
            "leechers": item.get("leechers", 0),
            "indexer": item_indexer or "Unknown",
            "publishDate": item.get("publishDate"),
            "magnetUrl": magnet,
            "downloadUrl": download_url,
            "infoHash": info_hash.lower() if info_hash else "",
            "infoUrl": item.get("infoUrl"),
            "categories": [c.get("name", "") for c in raw_cats],
            "mediaType": media_type,
        })

    if skipped_category:
        logger.info(
            "Prowlarr skipped %s results (category filter) for query %r",
            skipped_category,
            query[:80],
        )
    logger.info(
        "Prowlarr search done: %s book-related results (limit=%s, %.1fs)",
        len(results),
        limit,
        elapsed,
    )

    _cache[key] = (now, results)
    return results
