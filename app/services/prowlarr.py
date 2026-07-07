import logging
import re
import time
from typing import Any

import httpx
from app.config import get_settings
from app.services.real_debrid import extract_info_hash

logger = logging.getLogger(__name__)
settings = get_settings()

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
    r"\.(mp4|mkv|avi|wmv|flv|webm|exe|msi|iso)\b"
    r"|\b(?:1080p|720p|2160p|4k|x264|x265|hevc|bluray|web-dl|webrip)\b"
    r"|\b(?:season|s\d{2}e\d{2}|complete\s+series)\b"
    r"|\b(?:pre-?activated|crack|keygen|ftuapps)\b",
    re.IGNORECASE,
)


def _is_builtin_trusted_indexer(indexer: str) -> bool:
    return _is_audiobookbay_indexer(indexer) or _is_knaben_indexer(indexer)


def is_book_related(
    categories: list[dict],
    title: str = "",
    indexer: str = "",
) -> bool:
    """Return True if the result is potentially a book (audiobook or ebook)."""
    if _is_audiobookbay_indexer(indexer):
        return True
    if _NON_BOOK_TITLE.search(title):
        return False
    if AUDIOBOOK_KEYWORDS.search(title) or EBOOK_KEYWORDS.search(title):
        return True
    if _is_knaben_indexer(indexer):
        # Knaben indexes everything — require book signals, not blind trust.
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
) -> tuple[list[dict], dict[str, int]]:
    """Search ABB and Knaben separately so Knaben cannot crowd out ABB in the shared limit."""
    import asyncio
    from app.services.download_discovery import merge_indexer_results

    abb_limit = max(25, int(abb_limit if abb_limit is not None else settings.prowlarr_abb_search_limit))
    knaben_limit = max(25, int(knaben_limit if knaben_limit is not None else settings.prowlarr_search_limit))
    timeout = max(15, int(timeout if timeout is not None else settings.scraper_prowlarr_timeout))

    async def _abb() -> list[dict]:
        iid = await get_audiobookbay_indexer_id()
        if not iid:
            return []
        return await search(query, indexer_ids=[iid], limit=abb_limit, timeout=timeout)

    async def _knaben() -> list[dict]:
        ids = await get_knaben_indexer_ids()
        if not ids:
            return []
        return await search(query, indexer_ids=ids, limit=knaben_limit, timeout=timeout)

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


async def search_audiobookbay_multi(queries: list[str]) -> list[dict[str, Any]]:
    """Search only ABB so results are not dropped by the global Prowlarr result cap."""
    iid = await get_audiobookbay_indexer_id()
    if not iid:
        return []
    limit = max(25, int(settings.prowlarr_abb_search_limit))
    return await search_multi(queries, indexer_ids=[iid], limit=limit)


async def search_trusted_indexers_multi(queries: list[str]) -> list[dict[str, Any]]:
    """Search ABB, Knaben, and other trusted indexers (not every tracker in Prowlarr)."""
    indexer_ids = await get_trusted_indexer_ids()
    if not indexer_ids:
        return []
    per_call = max(50, int(settings.prowlarr_abb_search_limit))
    return await search_multi(queries, indexer_ids=indexer_ids, limit=per_call)


async def search_multi(
    queries: list[str],
    categories: list[int] | None = None,
    indexer_ids: list[int] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Run several Prowlarr searches and merge by info hash (best seeders kept)."""
    import asyncio

    if not queries:
        return []
    if len(queries) == 1:
        return await search(queries[0], categories=categories, indexer_ids=indexer_ids, limit=limit)

    gathered = await asyncio.gather(
        *[
            search(q, categories=categories, indexer_ids=indexer_ids, limit=limit)
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

        if not is_book_related(raw_cats, title=item_title, indexer=item_indexer):
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

        size = item.get("size", 0)
        media_type = detect_media_type(item_title, raw_cats, size)
        if (
            _is_audiobookbay_indexer(item_indexer)
            and media_type == "unknown"
            and size > SIZE_AUDIOBOOK_MIN
        ):
            media_type = "audiobook"

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
