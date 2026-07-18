"""ISBNdb API — book metadata for titles missing from the local Open Library dump.

Docs: https://isbndb.com/apidocs/v2
Base URL: https://api2.isbndb.com
Auth header: Authorization: <REST_KEY>
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

API_BASE = "https://api2.isbndb.com"
API_KEY_SETTING = "integrations.isbndb_api_key"

_ISBN_DIGITS_RE = re.compile(r"^[\dX]{10}$|^97[89]\d{10}$", re.I)


async def get_api_key() -> str:
    """Resolve ISBNdb key: admin override first, then env."""
    from app.services import app_settings

    env_key = (settings.isbndb_api_key or "").strip()
    return (await app_settings.get_setting(API_KEY_SETTING, default=env_key)).strip()


def _normalize_book(raw: dict) -> dict | None:
    """Map an ISBNdb book object into the store's BookSummary-shaped dict."""
    if not isinstance(raw, dict):
        return None
    isbn13 = (raw.get("isbn13") or "").strip()
    isbn10 = (raw.get("isbn") or raw.get("isbn10") or "").strip()
    title = (raw.get("title") or raw.get("title_long") or "").strip()
    if not title:
        return None

    # Stable id — prefer ISBN-13 so catalog matches survive re-searches.
    if isbn13:
        volume_id = f"ISBN:{isbn13}"
    elif isbn10:
        volume_id = f"ISBN:{isbn10}"
    else:
        # Rare: no ISBN — hash title+author into a stable-ish id.
        author0 = ""
        authors_raw = raw.get("authors") or []
        if isinstance(authors_raw, list) and authors_raw:
            author0 = str(authors_raw[0])
        slug = re.sub(r"[^a-z0-9]+", "-", f"{title}-{author0}".lower()).strip("-")[:80]
        volume_id = f"ISBN:title:{slug or 'unknown'}"

    authors: list[str] = []
    for a in raw.get("authors") or []:
        if isinstance(a, str) and a.strip():
            authors.append(a.strip())
        elif isinstance(a, dict):
            name = (a.get("name") or "").strip()
            if name:
                authors.append(name)

    subjects = raw.get("subjects") or []
    categories = [s for s in subjects if isinstance(s, str) and s.strip()][:8]

    cover = (raw.get("image") or raw.get("image_original") or "").strip()
    if cover.startswith("http://"):
        cover = "https://" + cover[7:]

    synopsis = (raw.get("synopsis") or raw.get("overview") or "").strip()
    published = (raw.get("date_published") or raw.get("publish_date") or "").strip()
    publisher = (raw.get("publisher") or "").strip()
    pages = raw.get("pages") or raw.get("pages_number") or 0
    try:
        page_count = int(pages) if pages else 0
    except (TypeError, ValueError):
        page_count = 0

    language = (raw.get("language") or "").strip() or "en"

    return {
        "id": volume_id,
        "volumeId": volume_id,
        "title": title,
        "subtitle": (raw.get("title_long") or "").strip() if raw.get("title_long") != title else "",
        "authors": authors,
        "publisher": publisher,
        "publishedDate": published,
        "description": synopsis,
        "pageCount": page_count,
        "categories": categories,
        "mainCategory": categories[0] if categories else "",
        "averageRating": 0,
        "ratingsCount": 0,
        "language": language,
        "coverUrl": cover,
        "coverUrlLarge": cover,
        "isbn10": isbn10,
        "isbn13": isbn13,
        "previewLink": "",
        "infoLink": f"https://isbndb.com/book/{isbn13 or isbn10}" if (isbn13 or isbn10) else "",
        "source": "isbndb",
        "industryIdentifiers": [
            *( [{"type": "ISBN_13", "identifier": isbn13}] if isbn13 else [] ),
            *( [{"type": "ISBN_10", "identifier": isbn10}] if isbn10 else [] ),
        ],
    }


async def _get(path: str, *, params: dict | None = None) -> dict | None:
    api_key = await get_api_key()
    if not api_key:
        return None
    url = f"{API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": api_key,
                    "Accept": "application/json",
                },
                params=params or {},
            )
            if resp.status_code == 404:
                return None
            if resp.status_code == 401:
                logger.warning("ISBNdb auth failed — check API key")
                return None
            if resp.status_code == 429:
                logger.warning("ISBNdb rate limited")
                return None
            if resp.status_code >= 400:
                logger.debug("ISBNdb %s -> %s: %s", path, resp.status_code, resp.text[:160])
                return None
            return resp.json()
    except Exception as e:
        logger.debug("ISBNdb request failed %s: %s", path, e)
        return None


async def lookup_isbn(isbn: str) -> dict | None:
    """Fetch one book by ISBN-10/13."""
    digits = "".join(c for c in (isbn or "").upper() if c.isdigit() or c == "X")
    if not _ISBN_DIGITS_RE.match(digits):
        return None
    data = await _get(f"/book/{digits}")
    if not data:
        return None
    book = data.get("book") if isinstance(data.get("book"), dict) else data
    return _normalize_book(book) if isinstance(book, dict) else None


async def search_books(query: str, *, limit: int = 20, page: int = 1) -> dict[str, Any]:
    """Title/author search. Returns {books, totalItems}."""
    q = (query or "").strip()
    if not q or not await get_api_key():
        return {"books": [], "totalItems": 0}

    # Digit-only queries → ISBN lookup.
    digits = "".join(c for c in q.upper() if c.isdigit() or c == "X")
    if _ISBN_DIGITS_RE.match(digits) and len(re.sub(r"\s+", "", q)) == len(digits):
        hit = await lookup_isbn(digits)
        return {"books": [hit] if hit else [], "totalItems": 1 if hit else 0}

    encoded = quote(q[:150], safe="")
    data = await _get(
        f"/books/{encoded}",
        params={
            "page": max(1, page),
            "pageSize": min(max(1, limit), 40),
            "column": "title",
        },
    )
    if not data:
        return {"books": [], "totalItems": 0}

    raw_books = data.get("books") or []
    books: list[dict] = []
    for raw in raw_books:
        norm = _normalize_book(raw) if isinstance(raw, dict) else None
        if norm:
            books.append(norm)

    total = int(data.get("total") or data.get("totalItems") or len(books))
    return {"books": books[:limit], "totalItems": total}


async def get_volume(volume_id: str) -> dict | None:
    """Resolve an ISBN:* store id via ISBNdb."""
    if not volume_id.startswith("ISBN:"):
        return None
    key = volume_id[5:]
    if key.startswith("title:"):
        # Best-effort: search by the slug words.
        words = key[6:].replace("-", " ").strip()
        if not words:
            return None
        result = await search_books(words, limit=1)
        books = result.get("books") or []
        return books[0] if books else None
    return await lookup_isbn(key)
