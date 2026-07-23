"""Direct Knaben API search.

Prowlarr's Knaben plugin drops any hit where the database API reports seeders=0.
Knaben's cached API often shows 0 seeders for audiobooks that knaben.org live
search lists with dozens — so Prowlarr misses most book results. We call
api.knaben.org directly and keep those rows (magnets are still valid).
"""
from __future__ import annotations

import asyncio
import logging
import re
import string
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.services.prowlarr import detect_media_type
from app.services.real_debrid import extract_info_hash

logger = logging.getLogger(__name__)
settings = get_settings()

API_URL = "https://api.knaben.org/v1"
RSS_URL = "https://rss.knaben.org"
INDEXER_NAME = "Knaben"
PAGE_SIZE = 100
MAX_PAGES_CAP = 10  # user-facing searches only
# Knaben uses Elasticsearch — from + size must be <= 10_000 per query.
CRAWL_MAX_OFFSET = 9900
CRAWL_SHARD_ALPHABET = string.ascii_lowercase + string.digits
MAX_SHARD_DEPTH = 2  # browse "" then single-char then two-char when capped

# Knaben categoryId → Torznab-style ids for media-type heuristics
_TORZNAB_CAT_MAP: dict[int, int] = {
    1_003_000: 3030,  # Audiobook
    1_001_000: 3010,  # MP3
    1_002_000: 3040,  # Lossless
    9_001_000: 7020,  # EBook
    9_000_000: 7000,  # Books
}

# Knaben only exposes two book-specific categories we care about.
# 1_001_000 (MP3) and 1_002_000 (Lossless) are music; 9_000_000 (Books) is broader than ebooks.
KNABEN_AUDIOBOOK_CATEGORY = 1_003_000
KNABEN_EBOOK_CATEGORY = 9_001_000
KNABEN_BOOK_CATEGORY_IDS = frozenset({
    KNABEN_AUDIOBOOK_CATEGORY,
    KNABEN_EBOOK_CATEGORY,
})
KNABEN_DEFAULT_CATEGORIES = (KNABEN_AUDIOBOOK_CATEGORY, KNABEN_EBOOK_CATEGORY)
KNABEN_MUSIC_CATEGORY_IDS = frozenset({1_001_000, 1_002_000})
# Knaben top-level non-book categories (from knaben.org browse / public API docs).
KNABEN_TV_CATEGORY = 2_000_000
KNABEN_MOVIES_CATEGORY = 3_000_000
KNABEN_ANIME_CATEGORY = 6_000_000
# XXX / adult branch (Video, ImageSet, Games, Hentai, Other).
KNABEN_XXX_CATEGORY = 5_000_000
KNABEN_ADULT_CATEGORY_IDS = frozenset({
    KNABEN_XXX_CATEGORY,
    5_001_000,
    5_002_000,
    5_003_000,
    5_004_000,
    5_005_000,
})


def knaben_title_looks_like_music(title: str) -> bool:
    from app.services.rss_content_filters import title_looks_like_music

    return title_looks_like_music(title)

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
CACHE_TTL = 300


@dataclass(frozen=True)
class KnabenSearchOptions:
    """Parameters for one Knaben API search (supports pagination)."""

    query: str = ""
    categories: tuple[int, ...] = ()
    order_by: str = "date"  # date surfaces long tail; seeders for live search
    order_direction: str = "desc"
    search_type: str = "50%"
    search_field: str = "title"
    hide_unsafe: bool = True
    hide_xxx: bool = True

    def cache_key(self) -> str:
        cats = ",".join(str(c) for c in self.categories)
        return (
            f"{self.query.lower().strip()}|{cats}|{self.order_by}|{self.order_direction}"
            f"|{self.search_type}|{self.search_field}|{int(self.hide_unsafe)}|{int(self.hide_xxx)}"
        )

    def label(self) -> str:
        q = self.query.strip() or "*"
        if self.categories:
            return f"cat:{self.categories[0]}:{self.order_by}:{q[:40]}"
        return f"{self.order_by}:{q[:60]}"


@dataclass
class KnabenFullCrawlState:
    """Persisted progress for exhaustive category sweeps (audiobook then ebook)."""

    categories: list[int]
    category_idx: int = 0
    shards: list[str] | None = None
    shard_idx: int = 0
    offset: int = 0
    expanded_shards: list[str] | None = None
    phase: str = "full"  # full | maintenance

    def __post_init__(self) -> None:
        if self.shards is None:
            self.shards = default_category_shards()
        if self.expanded_shards is None:
            self.expanded_shards = []

    @property
    def active_category(self) -> int | None:
        if self.phase != "full" or self.category_idx >= len(self.categories):
            return None
        return self.categories[self.category_idx]

    @property
    def active_shard(self) -> str | None:
        if not self.shards or self.shard_idx >= len(self.shards):
            return None
        return self.shards[self.shard_idx]

    def category_label(self, category_id: int) -> str:
        if category_id == KNABEN_AUDIOBOOK_CATEGORY:
            return "audiobook"
        if category_id == KNABEN_EBOOK_CATEGORY:
            return "ebook"
        return str(category_id)

    def progress_summary(self) -> dict[str, Any]:
        cat = self.active_category
        shard = self.active_shard
        shard_label = "browse all" if shard == "" else f"title slice «{shard}»"
        return {
            "phase": self.phase,
            "category": self.category_label(cat) if cat else None,
            "categoryIndex": self.category_idx,
            "categoriesTotal": len(self.categories),
            "shard": shard,
            "shardLabel": shard_label,
            "shardIndex": self.shard_idx,
            "shardsTotal": len(self.shards or []),
            "offset": self.offset,
            "pagesPerJob": None,
        }


def default_category_shards() -> list[str]:
    """Browse whole category first, then single-character title prefixes."""
    return [""] + list(CRAWL_SHARD_ALPHABET)


def new_full_crawl_state() -> KnabenFullCrawlState:
    """Audiobook category only — Knaben ebook browse is mostly huge packs."""
    return KnabenFullCrawlState(categories=[KNABEN_AUDIOBOOK_CATEGORY])


def _normalize_crawl_categories(categories: list[int]) -> list[int]:
    out = [int(c) for c in categories if int(c) != KNABEN_EBOOK_CATEGORY]
    return out or [KNABEN_AUDIOBOOK_CATEGORY]


def crawl_state_from_json(data: dict[str, Any] | None) -> KnabenFullCrawlState:
    if not data:
        return new_full_crawl_state()
    categories = _normalize_crawl_categories(list(data.get("categories") or []))
    category_idx = int(data.get("category_idx") or data.get("categoryIndex") or 0)
    if category_idx >= len(categories):
        category_idx = 0
    phase = str(data.get("phase") or "full")
    if phase == "full" and category_idx >= len(categories):
        phase = "maintenance"
    return KnabenFullCrawlState(
        categories=categories,
        category_idx=category_idx,
        shards=list(data.get("shards") or default_category_shards()),
        shard_idx=int(data.get("shard_idx") or data.get("shardIndex") or 0),
        offset=int(data.get("offset") or 0),
        expanded_shards=list(data.get("expanded_shards") or data.get("expandedShards") or []),
        phase=phase,
    )


def crawl_state_to_json(state: KnabenFullCrawlState) -> dict[str, Any]:
    return {
        "categories": list(state.categories),
        "category_idx": state.category_idx,
        "shards": list(state.shards or []),
        "shard_idx": state.shard_idx,
        "offset": state.offset,
        "expanded_shards": list(state.expanded_shards or []),
        "phase": state.phase,
    }


def category_shards_for_depth(depth: int) -> list[str]:
    if depth <= 0:
        return [""]
    chars = list(CRAWL_SHARD_ALPHABET)
    if depth == 1:
        return [""] + chars
    if depth == 2:
        return [a + b for a in chars for b in chars]
    return chars


def _total_value(total: Any) -> int:
    if isinstance(total, dict):
        try:
            return int(total.get("value") or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(total, int):
        return total
    return 0


def _total_is_capped(total: Any) -> bool:
    if not isinstance(total, dict):
        return False
    relation = str(total.get("relation") or "").lower()
    value = _total_value(total)
    return relation == "gte" and value >= 10_000


def _total_needs_shard_expansion(total: Any) -> bool:
    """True when a shard has more hits than Elasticsearch allows per query window."""
    if _total_is_capped(total):
        return True
    return _total_value(total) > CRAWL_MAX_OFFSET + PAGE_SIZE


def expand_crawl_shard(shards: list[str], shard_idx: int, parent: str) -> list[str]:
    """Insert two-character child shards when a single-character slice hits the 10k window."""
    if not parent or len(parent) >= MAX_SHARD_DEPTH:
        return shards
    children = [parent + ch for ch in CRAWL_SHARD_ALPHABET]
    out = list(shards)
    insert_at = shard_idx + 1
    for child in reversed(children):
        if child not in out:
            out.insert(insert_at, child)
    return out


def advance_crawl_state(state: KnabenFullCrawlState, *, next_offset: int, shard_exhausted: bool) -> None:
    """Move offset/shard/category after a crawl batch."""
    if state.phase != "full" or not state.shards:
        return

    if not shard_exhausted:
        state.offset = next_offset
        return

    state.shard_idx += 1
    state.offset = 0

    if state.shard_idx < len(state.shards):
        return

    state.category_idx += 1
    state.shard_idx = 0
    state.shards = default_category_shards()
    state.expanded_shards = []

    if state.category_idx >= len(state.categories):
        state.phase = "maintenance"
        logger.info("Knaben full category crawl complete — switching to RSS maintenance")


def build_knaben_crawl_queue() -> list[KnabenSearchOptions]:
    """Legacy helper kept for tests; production uses KnabenFullCrawlState sweeps."""
    return [
        KnabenSearchOptions(query="", categories=(KNABEN_AUDIOBOOK_CATEGORY,), order_by="date"),
    ]


def _normalize_category_ids(raw: Any) -> list[int]:
    """Knaben API returns categoryId as int or list depending on query type."""
    if raw is None:
        return []
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, list):
        out: list[int] = []
        for item in raw:
            if isinstance(item, int):
                out.append(item)
            elif isinstance(item, str) and item.isdigit():
                out.append(int(item))
        return out
    if isinstance(raw, str) and raw.isdigit():
        return [int(raw)]
    return []


def _primary_category_id(raw: Any) -> int | None:
    ids = _normalize_category_ids(raw)
    return ids[0] if ids else None


def _is_knaben_native_book_category(category_id: int | None) -> bool:
    if category_id is None:
        return False
    return int(category_id) in KNABEN_BOOK_CATEGORY_IDS


def _reject_non_book_category(native_ids: list[int]) -> bool:
    """True when any native Knaben category is music / video / adult."""
    for cid in native_ids:
        c = int(cid)
        if c in KNABEN_MUSIC_CATEGORY_IDS:
            return True
        if c in KNABEN_ADULT_CATEGORY_IDS:
            return True
        # Top-level ranges: TV 2xxx000, Movies 3xxx000, Anime 6xxx000 (not literature).
        top = (c // 1_000_000) * 1_000_000
        if top in (KNABEN_TV_CATEGORY, KNABEN_MOVIES_CATEGORY):
            return True
        if top == KNABEN_ANIME_CATEGORY and c != 6_006_000:  # keep Anime Literature
            return True
        if top == KNABEN_XXX_CATEGORY:
            return True
    return False


def _knaben_hit_is_book(hit: dict[str, Any]) -> bool:
    """Only keep hits tagged Audiobook or EBook on Knaben."""
    from app.services.rss_content_filters import title_is_non_book, title_looks_adult

    native_ids = _normalize_category_ids(hit.get("categoryId"))
    if _reject_non_book_category(native_ids):
        return False
    cat_label = (hit.get("category") or "").strip().lower()
    if cat_label and (
        cat_label.startswith("xxx")
        or "hentai" in cat_label
        or cat_label in ("adult", "porn")
        or "/xxx" in cat_label
    ):
        return False
    title = (hit.get("title") or "").strip()
    # RSS forces an audiobook category id — title filters catch miscategorized porn/music/movies.
    if title_is_non_book(title) or title_looks_adult(title):
        return False
    return any(cid in KNABEN_BOOK_CATEGORY_IDS for cid in native_ids)


def _map_categories(category_ids: list[int] | int | Any | None, category_label: str = "") -> list[dict]:
    ids = _normalize_category_ids(category_ids)
    out: list[dict] = []
    label = (category_label or "").strip()
    for cid in ids:
        mapped = _TORZNAB_CAT_MAP.get(int(cid))
        if mapped:
            out.append({"id": mapped, "name": label or str(cid)})
    if label and not out:
        out.append({"id": 0, "name": label})
    return out


def _hit_to_result(hit: dict[str, Any]) -> dict[str, Any] | None:
    title = (hit.get("title") or "").strip()
    if not title:
        return None

    size = int(hit.get("bytes") or 0)
    seeders = int(hit.get("seeders") or 0)
    peers = int(hit.get("peers") or 0)
    leechers = max(0, peers - seeders)
    native_cid = hit.get("categoryId")
    raw_cats = _map_categories(native_cid, hit.get("category") or "")
    media_type = detect_media_type(title, raw_cats, size)

    if not _knaben_hit_is_book(hit):
        return None

    from app.services.indexer_cache import ebook_size_acceptable
    from app.services.rss_content_filters import is_too_small_for_audiobook

    if is_too_small_for_audiobook(size, media_type):
        return None
    if not ebook_size_acceptable(media_type, size):
        return None

    magnet = (hit.get("magnetUrl") or "").strip()
    download_url = (hit.get("link") or "").strip() or None
    info_hash = (hit.get("hash") or "").lower()
    if not info_hash and magnet:
        parsed = extract_info_hash(magnet, None, download_url)
        if parsed:
            info_hash = parsed.lower()

    cat_names = [c.get("name", "") for c in raw_cats if c.get("name")]
    if not cat_names and hit.get("category"):
        cat_names = [str(hit["category"])]

    return {
        "title": title,
        "size": size,
        "seeders": seeders,
        "leechers": leechers,
        "indexer": INDEXER_NAME,
        "publishDate": hit.get("date"),
        "magnetUrl": magnet or None,
        "downloadUrl": download_url,
        "infoHash": info_hash,
        "infoUrl": hit.get("details"),
        "categories": cat_names,
        "mediaType": media_type,
    }


def _hits_to_results(hits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    skipped = 0
    for hit in hits:
        row = _hit_to_result(hit)
        if row:
            results.append(row)
        else:
            skipped += 1
    return results, skipped


async def _fetch_page_raw(
    opts: KnabenSearchOptions,
    *,
    offset: int,
    timeout: int,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "order_by": opts.order_by,
        "order_direction": opts.order_direction,
        "from": offset,
        "size": page_size,
        "hide_unsafe": opts.hide_unsafe,
        "hide_xxx": opts.hide_xxx,
        "search_type": opts.search_type,
        "search_field": opts.search_field,
    }
    q = opts.query.strip()
    if q:
        body["query"] = q
    if opts.categories:
        body["categories"] = list(opts.categories)

    async with httpx.AsyncClient() as client:
        resp = await client.post(API_URL, json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        return {"hits": [], "total": None}
    hits = data.get("hits")
    if not isinstance(hits, list):
        hits = []
    return {"hits": hits, "total": data.get("total"), "error": data.get("error")}


async def _fetch_page(opts: KnabenSearchOptions, *, offset: int, timeout: int) -> list[dict[str, Any]]:
    data = await _fetch_page_raw(opts, offset=offset, timeout=timeout)
    hits = data.get("hits")
    return hits if isinstance(hits, list) else []


async def probe_category_shard_total(category_id: int, query: str, *, timeout: int = 30) -> int:
    opts = KnabenSearchOptions(query=query, categories=(category_id,), order_by="date")
    data = await _fetch_page_raw(opts, offset=0, timeout=timeout, page_size=0)
    return _total_value(data.get("total"))


async def maybe_expand_crawl_shard(state: KnabenFullCrawlState, *, timeout: int) -> None:
    """When a shard hits Knaben's 10k result window, queue finer-grained child shards."""
    cat = state.active_category
    shard = state.active_shard
    if cat is None or shard is None or state.offset != 0:
        return
    if not shard or shard in (state.expanded_shards or []):
        return

    opts = KnabenSearchOptions(query=shard, categories=(cat,), order_by="date")
    data = await _fetch_page_raw(opts, offset=0, timeout=timeout, page_size=0)
    total = data.get("total")
    if not _total_needs_shard_expansion(total):
        return

    state.shards = expand_crawl_shard(state.shards or [], state.shard_idx, shard)
    state.expanded_shards = list(state.expanded_shards or []) + [shard]
    logger.info(
        "Knaben crawl shard %r capped at ~10k — expanded to %s child shards (category %s)",
        shard or "*",
        len(state.shards or []) - state.shard_idx - 1,
        state.category_label(cat),
    )


async def crawl_full_category_batch(
    state: KnabenFullCrawlState,
    *,
    max_pages: int,
    timeout: int,
) -> list[dict[str, Any]]:
    """Fetch the next pages for the active category shard."""
    if state.phase != "full":
        return []

    cat = state.active_category
    shard = state.active_shard
    if cat is None or shard is None:
        return []

    await maybe_expand_crawl_shard(state, timeout=timeout)

    opts = KnabenSearchOptions(query=shard, categories=(cat,), order_by="date")
    merged: dict[str, dict[str, Any]] = {}
    last_page_size = 0
    offset = state.offset

    for _ in range(max(1, max_pages)):
        if offset > CRAWL_MAX_OFFSET:
            break
        page = await _fetch_page_raw(opts, offset=offset, timeout=timeout)
        hits = page.get("hits") or []
        if page.get("error"):
            logger.warning("Knaben crawl page error cat=%s shard=%r offset=%s: %s", cat, shard, offset, page["error"])
            break
        if not hits:
            last_page_size = 0
            break

        results, _skipped = _hits_to_results(hits)
        for row in results:
            key = (row.get("infoHash") or "").lower()
            if not key:
                key = row.get("magnetUrl") or row.get("downloadUrl") or f"{row.get('title')}|{row.get('indexer')}"
            prev = merged.get(key)
            if not prev or (row.get("seeders") or 0) > (prev.get("seeders") or 0):
                merged[key] = row

        last_page_size = len(hits)
        offset += len(hits)
        if last_page_size < PAGE_SIZE:
            break

    shard_exhausted = (
        last_page_size == 0
        or last_page_size < PAGE_SIZE
        or offset > CRAWL_MAX_OFFSET
    )
    advance_crawl_state(state, next_offset=offset, shard_exhausted=shard_exhausted)
    out = list(merged.values())
    logger.info(
        "Knaben full crawl: %s shard=%r → %s unique (next %s)",
        state.category_label(cat),
        shard or "*",
        len(out),
        state.progress_summary(),
    )
    return out


def _rss_url_for_category(category_id: int, *, size: int = 150) -> str:
    # hide_unsafe + hide_xxx — adult torrents still leak into audiobook RSS via
    # miscategorization; title filters catch those. The flags help for tagged XXX.
    return f"{RSS_URL}//{category_id}/{size}/hide_unsafe/hide_xxx"


def _parse_rss_size_bytes(text: str) -> int:
    m = re.search(r"Size:\s*([\d.,]+)\s*(KB|MB|GB|TB)", text or "", re.IGNORECASE)
    if not m:
        return 0
    amount = float(m.group(1).replace(",", ""))
    unit = m.group(2).upper()
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 1)
    return int(amount * mult)


def _rss_item_to_hit(item: dict[str, str], *, category_id: int) -> dict[str, Any] | None:
    title = (item.get("title") or "").strip()
    if not title:
        return None

    guid = (item.get("guid") or item.get("id") or "").strip().lower()
    description = item.get("description") or item.get("summary") or ""
    magnet_m = re.search(r"(magnet:\?[^\s<\"']+)", description, re.IGNORECASE)
    magnet = magnet_m.group(1) if magnet_m else ""
    info_hash = guid if len(guid) == 40 and re.fullmatch(r"[0-9a-f]{40}", guid) else ""
    if not info_hash and magnet:
        parsed = extract_info_hash(magnet, None, None)
        info_hash = (parsed or "").lower()

    category_label = "Audiobook" if category_id == KNABEN_AUDIOBOOK_CATEGORY else "EBook"
    return {
        "title": title,
        "bytes": _parse_rss_size_bytes(description),
        "seeders": 0,
        "peers": 0,
        "categoryId": category_id,
        "category": category_label,
        "magnetUrl": magnet or None,
        "hash": info_hash,
        "details": item.get("link"),
        "date": item.get("published") or item.get("updated"),
    }


def _parse_knaben_rss_xml(xml_text: str, *, category_id: int) -> list[dict[str, Any]]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Knaben RSS parse error: %s", e)
        return []

    channel = root.find("channel")
    if channel is None:
        channel = root

    results: list[dict[str, Any]] = []
    for item_el in channel.findall("item"):
        item: dict[str, str] = {}
        for child in item_el:
            tag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if tag == "guid" and not text:
                text = (child.get("isPermaLink") or child.attrib.get("isPermaLink") or "")
            if text:
                item[tag] = text
        hit = _rss_item_to_hit(item, category_id=category_id)
        if not hit:
            continue
        row = _hit_to_result(hit)
        if row:
            results.append(row)
    return results


async def fetch_rss_category(
    category_id: int,
    *,
    size: int = 150,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    url = _rss_url_for_category(category_id, size=size)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            xml_text = resp.text
    except Exception as e:
        logger.warning("Knaben RSS fetch failed for %s: %s", category_id, e)
        return []

    results = _parse_knaben_rss_xml(xml_text, category_id=category_id)
    logger.info("Knaben RSS cat=%s size=%s → %s book torrents", category_id, size, len(results))
    return results


async def poll_rss_feeds(*, size: int = 150, timeout: int = 30) -> list[dict[str, Any]]:
    """Fetch recent audiobook uploads from Knaben RSS (ebook RSS skipped)."""
    merged: dict[str, dict[str, Any]] = {}
    for cat_id in (KNABEN_AUDIOBOOK_CATEGORY,):
        batch = await fetch_rss_category(cat_id, size=size, timeout=timeout)
        for row in batch:
            key = (row.get("infoHash") or "").lower()
            if not key:
                key = row.get("magnetUrl") or row.get("downloadUrl") or f"{row.get('title')}|{row.get('indexer')}"
            prev = merged.get(key)
            if not prev or (row.get("seeders") or 0) > (prev.get("seeders") or 0):
                merged[key] = row
    return list(merged.values())


async def _fetch_hits_paginated(
    opts: KnabenSearchOptions,
    *,
    limit: int,
    timeout: int,
) -> list[dict[str, Any]]:
    max_pages = min(MAX_PAGES_CAP, max(1, (limit + PAGE_SIZE - 1) // PAGE_SIZE))
    all_hits: list[dict[str, Any]] = []
    offset = 0

    for _ in range(max_pages):
        if len(all_hits) >= limit:
            break
        hits = await _fetch_page(opts, offset=offset, timeout=timeout)
        if not hits:
            break
        all_hits.extend(hits)
        if len(hits) < PAGE_SIZE:
            break
        offset += len(hits)

    return all_hits[:limit]


async def search_with_options(
    opts: KnabenSearchOptions,
    *,
    limit: int | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    if limit is None:
        limit = max(50, int(settings.prowlarr_search_limit))
    timeout = max(15, int(timeout or settings.prowlarr_search_timeout))

    cache_key = opts.cache_key()
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1][:limit]

    t0 = time.perf_counter()
    try:
        hits = await _fetch_hits_paginated(opts, limit=limit, timeout=timeout)
    except Exception as e:
        logger.warning("Knaben API search failed for %s: %s", opts.label(), e)
        return []

    results, skipped = _hits_to_results(hits)
    elapsed = time.perf_counter() - t0
    if skipped:
        logger.info("Knaben skipped %s non-book hits for %s", skipped, opts.label())
    logger.info(
        "Knaben direct search: %s book-related results (%s raw hits, limit=%s, %.1fs) for %s",
        len(results),
        len(hits),
        limit,
        elapsed,
        opts.label(),
    )

    _cache[cache_key] = (now, results)
    return results[:limit]


async def search(
    query: str,
    *,
    limit: int | None = None,
    timeout: int | None = None,
    order_by: str = "seeders",
    categories: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    cats = categories if categories is not None else KNABEN_DEFAULT_CATEGORIES
    opts = KnabenSearchOptions(query=q, categories=cats, order_by=order_by)
    return await search_with_options(opts, limit=limit, timeout=timeout)


async def search_multi(
    queries: list[str],
    *,
    limit: int | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    if not queries:
        return []
    if len(queries) == 1:
        return await search(queries[0], limit=limit, timeout=timeout)

    per_query_limit = limit
    if limit is not None and limit > 0:
        per_query_limit = max(50, limit // max(1, len(queries)))

    gathered = await asyncio.gather(
        *[search(q, limit=per_query_limit, timeout=timeout) for q in queries],
        return_exceptions=True,
    )
    merged: dict[str, dict[str, Any]] = {}
    for i, batch in enumerate(gathered):
        if isinstance(batch, Exception):
            logger.warning("Knaben search query %r failed: %s", queries[i], batch)
            continue
        for row in batch:
            key = (row.get("infoHash") or "").lower()
            if not key:
                key = row.get("magnetUrl") or row.get("downloadUrl") or f"{row.get('title')}|{row.get('indexer')}"
            prev = merged.get(key)
            if not prev or (row.get("seeders") or 0) > (prev.get("seeders") or 0):
                merged[key] = row

    out = list(merged.values())
    if limit is not None and limit > 0:
        out = out[:limit]
    logger.info("Knaben multi-search: %s queries → %s unique results", len(queries), len(out))
    return out


async def crawl_tasks(
    specs: list[KnabenSearchOptions],
    *,
    limit: int | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Run dedicated Knaben crawl specs (category browse, alphabet slices, etc.)."""
    if not specs:
        return []
    if limit is None:
        limit = max(100, int(settings.prowlarr_search_limit))

    gathered = await asyncio.gather(
        *[search_with_options(spec, limit=limit, timeout=timeout) for spec in specs],
        return_exceptions=True,
    )
    merged: dict[str, dict[str, Any]] = {}
    for i, batch in enumerate(gathered):
        if isinstance(batch, Exception):
            logger.warning("Knaben crawl task %s failed: %s", specs[i].label(), batch)
            continue
        for row in batch:
            key = (row.get("infoHash") or "").lower()
            if not key:
                key = row.get("magnetUrl") or row.get("downloadUrl") or f"{row.get('title')}|{row.get('indexer')}"
            prev = merged.get(key)
            if not prev or (row.get("seeders") or 0) > (prev.get("seeders") or 0):
                merged[key] = row

    out = list(merged.values())
    logger.info("Knaben crawl: %s tasks → %s unique results", len(specs), len(out))
    return out
