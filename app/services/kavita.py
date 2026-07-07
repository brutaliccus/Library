import logging
import time
from pathlib import Path
from typing import Any

import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Kavita MangaFormat: Image=0, Archive=1, Unknown=2, Epub=3, Pdf=4
EBOOK_FORMATS = [3, 4]
PDF_FORMAT = 4
EPUB_FORMAT = 3

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300  # 5 minutes


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.kavita_api_key}


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = (time.time(), data)


def invalidate_cache() -> None:
    _cache.clear()


async def scan_all_libraries() -> None:
    if not settings.kavita_api_key:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.kavita_url}/api/Library/scan-all",
            headers=_headers(),
            timeout=60,
        )
        resp.raise_for_status()


async def scan_library(library_id: int | None = None) -> None:
    if not settings.kavita_api_key:
        return
    lid = library_id or settings.kavita_library_id
    if not lid:
        await scan_all_libraries()
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.kavita_url}/api/Library/scan",
            headers=_headers(),
            params={"libraryId": lid},
            timeout=60,
        )
        resp.raise_for_status()


async def search_library(query: str) -> list[dict]:
    """Search Kavita library; returns list of {title, author} for matching."""
    if not settings.kavita_api_key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.kavita_url}/api/Search/search",
                params={"queryString": query, "includeChapterAndFiles": True},
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    # Build a seriesId -> author map from chapters (which carry writer info)
    series_authors: dict[int, str] = {}
    for ch in data.get("chapters", []) or []:
        writers = ch.get("writers") or []
        if writers:
            author_name = writers[0].get("name", "")
            # Chapters reference their series via volumeId -> we match by title instead
            ch_title = ch.get("title") or ch.get("titleName") or ""
            if author_name and ch_title:
                series_authors[ch_title] = author_name

    items: list[dict] = []
    seen: set[str] = set()
    for series in data.get("series", []) or []:
        name = series.get("name") or series.get("localizedName") or series.get("originalName") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        author = ""
        # Try to find author from matching chapter data
        for ch_title, ch_author in series_authors.items():
            if ch_title == name or name in ch_title or ch_title in name:
                author = ch_author
                break
        items.append({"title": name, "author": author})
    return items


async def health_check() -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.kavita_url}/api/health",
                timeout=5,
            )
            return resp.status_code == 200
    except Exception:
        return False


async def get_all_series(library_id: int | None = None, formats: list[int] | None = None) -> list[dict]:
    """Fetch all series from Kavita; optionally filter by format (e.g. EBOOK_FORMATS for ebooks)."""
    if not settings.kavita_api_key:
        return []
    lid = library_id or settings.kavita_library_id
    cache_key = f"kavita_series:{lid}:{','.join(map(str, formats or []))}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.kavita_url}/api/Series/all-v2",
                headers={**_headers(), "Content-Type": "application/json"},
                json={},
                params={"PageSize": 0},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        # Kavita returns array directly, or sometimes {items: [...]} for pagination
        items = data if isinstance(data, list) else (data.get("items", []) if isinstance(data, dict) else [])
        if not isinstance(items, list):
            items = []
        if formats:
            items = [s for s in items if s.get("format") in formats]
        _cache_set(cache_key, items)
        return items
    except Exception as e:
        logger.warning("Failed to fetch Kavita series: %s", e)
        return []


async def get_series_volumes(series_id: int) -> list[dict]:
    """Get volumes and chapters for a series."""
    if not settings.kavita_api_key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.kavita_url}/api/Series/volumes",
                params={"seriesId": series_id},
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Failed to fetch Kavita volumes for series %s: %s", series_id, e)
        return []


async def get_chapter_file_path(chapter_id: int) -> Path | None:
    """Resolve a Kavita chapter id to a local ebook file path."""
    info = await get_book_info(chapter_id)
    if not info:
        return None
    series_id = info.get("seriesId")
    if not series_id:
        return None
    volumes = await get_series_volumes(series_id)
    for vol in volumes:
        for ch in vol.get("chapters") or []:
            if ch.get("id") != chapter_id:
                continue
            files = ch.get("files") or []
            if not files:
                return None
            kavita_path = files[0].get("filePath") or ""
            return _kavita_path_to_local(kavita_path)
    return None


def _kavita_path_to_local(kavita_path: str) -> Path | None:
    """Map Kavita library paths (/manga/...) to the mounted ebook directory."""
    if not kavita_path:
        return None
    rel = kavita_path.strip().lstrip("/")
    if rel.startswith("manga/"):
        rel = rel[6:]
    return Path(settings.ebook_dir) / rel


async def get_book_info(chapter_id: int) -> dict | None:
    """Get EPUB/PDF metadata for the reader (caches the file on Kavita)."""
    if not settings.kavita_api_key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.kavita_url}/api/Book/{chapter_id}/book-info",
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Failed to fetch Kavita book-info for chapter %s: %s", chapter_id, e)
        return None


async def get_book_chapters(chapter_id: int) -> list[dict]:
    """Get TOC / page mappings for an EPUB chapter."""
    if not settings.kavita_api_key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.kavita_url}/api/Book/{chapter_id}/chapters",
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Failed to fetch Kavita chapters for %s: %s", chapter_id, e)
        return []


async def get_book_page(chapter_id: int, page: int) -> str | None:
    """Get a single page HTML for an EPUB chapter."""
    if not settings.kavita_api_key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.kavita_url}/api/Book/{chapter_id}/book-page",
                params={"page": page},
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        logger.warning("Failed to fetch Kavita book-page %s: %s", chapter_id, e)
        return None


async def get_book_resources(chapter_id: int, file_path: str) -> bytes | None:
    """Fetch a resource (image, font, etc.) from within an EPUB."""
    if not settings.kavita_api_key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.kavita_url}/api/Book/{chapter_id}/book-resources",
                params={"file": file_path},
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.warning("Failed to fetch Kavita resource %s: %s", file_path, e)
        return None
