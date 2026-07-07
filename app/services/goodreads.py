import asyncio
import logging
import re
import time
from urllib.parse import quote
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 86400  # 24 hours -- ratings barely change

_semaphore = asyncio.Semaphore(2)
_last_ts: float = 0.0
_MIN_GAP = 1.0  # be polite to Goodreads


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = (time.time(), data)
    if len(_cache) > 2000:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]


_RATING_RE = re.compile(
    r'ratingValue["\s:]+(\d+\.?\d*)',
)
_COUNT_RE = re.compile(
    r'ratingCount["\s:]+(\d[\d,]*)',
)
_RATING_TEXT_RE = re.compile(
    r'(\d\.\d{2})\s+Rating details',
)
_VOTES_RE = re.compile(
    r'([\d,]+)\s+ratings',
)
_REVIEWS_RE = re.compile(
    r'([\d,]+)\s+reviews',
)
_GR_URL_RE = re.compile(
    r'href="(/book/show/\d+[^"]*)"',
)
_SERIES_RE = re.compile(
    r"###\s*(.+?)\s*#\d+",
    re.IGNORECASE,
)
_SERIES_ALT_RE = re.compile(
    r"Book\s+\d+\s+in\s+(?:the\s+)?(.+?)\s+series",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _parse_rating(html: str) -> dict | None:
    """Extract rating data from Goodreads HTML."""
    rating = None
    count = None
    review_count = None

    m = _RATING_RE.search(html)
    if m:
        try:
            rating = float(m.group(1))
        except ValueError:
            pass

    m = _COUNT_RE.search(html)
    if m:
        try:
            count = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    if rating is None:
        m = _RATING_TEXT_RE.search(html)
        if m:
            try:
                rating = float(m.group(1))
            except ValueError:
                pass

    if count is None:
        m = _VOTES_RE.search(html)
        if m:
            try:
                count = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

    m = _REVIEWS_RE.search(html)
    if m:
        try:
            review_count = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    if rating is not None and count is not None:
        return {
            "goodreadsRating": round(rating, 2),
            "goodreadsCount": count,
            "goodreadsReviewCount": review_count or 0,
        }
    return None


async def _fetch_page(url: str) -> str | None:
    """Fetch a Goodreads page with rate limiting."""
    global _last_ts
    async with _semaphore:
        gap = _MIN_GAP - (time.monotonic() - _last_ts)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            async with httpx.AsyncClient(follow_redirects=True, headers=HEADERS) as client:
                resp = await client.get(url, timeout=15)
                _last_ts = time.monotonic()
                if resp.status_code == 200:
                    return resp.text
                logger.debug("Goodreads returned %d for %s", resp.status_code, url)
                return None
        except Exception:
            _last_ts = time.monotonic()
            logger.debug("Goodreads fetch failed for %s", url)
            return None


async def get_rating_by_isbn(isbn: str) -> dict | None:
    """Look up Goodreads rating by ISBN."""
    if not isbn:
        return None

    cache_key = f"gr_isbn:{isbn}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached != "MISS" else None

    html = await _fetch_page(f"https://www.goodreads.com/book/isbn/{isbn}")
    if not html:
        html = await _fetch_page(
            f"https://www.goodreads.com/search?q={isbn}"
        )

    if not html:
        _cache_set(cache_key, "MISS")
        return None

    result = _parse_rating(html)
    _cache_set(cache_key, result or "MISS")
    return result


async def get_rating_by_title(title: str, author: str = "") -> dict | None:
    """Look up Goodreads rating by title search."""
    if not title:
        return None

    q = f"{title} {author}".strip()
    cache_key = f"gr_title:{q}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached != "MISS" else None

    html = await _fetch_page(
        f"https://www.goodreads.com/search?q={httpx.URL('', params={'q': q}).params['q']}"
    )
    if not html:
        _cache_set(cache_key, "MISS")
        return None

    result = _parse_rating(html)
    _cache_set(cache_key, result or "MISS")
    return result


async def get_rating(isbn13: str = "", isbn10: str = "", title: str = "", author: str = "") -> dict | None:
    """Try ISBN first, fall back to title search."""
    result = await get_rating_by_isbn(isbn13 or isbn10)
    if result:
        return result
    if title:
        return await get_rating_by_title(title, author)
    return None


def _parse_series(html: str) -> str | None:
    """Extract series name from Goodreads book page. E.g. '### White Trash Zombie #1' -> 'White Trash Zombie'."""
    m = _SERIES_RE.search(html)
    if m:
        return m.group(1).strip()
    m = _SERIES_ALT_RE.search(html)
    if m:
        return m.group(1).strip()
    return None


async def get_series(title: str, author: str = "") -> str | None:
    """Look up the canonical series name from Goodreads by book title + author."""
    if not title:
        return None

    q = f"{title} {author}".strip()
    cache_key = f"gr_series:{q}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached != "MISS" else None

    url = f"https://www.goodreads.com/search?q={quote(q)}"
    html = await _fetch_page(url)
    if not html:
        _cache_set(cache_key, "MISS")
        return None

    book_url = _GR_URL_RE.search(html)
    if not book_url:
        _cache_set(cache_key, "MISS")
        return None

    path = book_url.group(1).split("?")[0]
    page_url = f"https://www.goodreads.com{path}"
    html = await _fetch_page(page_url)
    if not html:
        _cache_set(cache_key, "MISS")
        return None

    series = _parse_series(html)
    _cache_set(cache_key, series or "MISS")
    return series
