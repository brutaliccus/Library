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


async def _conn() -> tuple[str, str, int]:
    """Effective Kavita URL/key/library (DB override → env)."""
    try:
        from app.services import instance_settings as inst

        return await inst.get_kavita_connection()
    except Exception:
        return settings.kavita_url, settings.kavita_api_key, settings.kavita_library_id


def _headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


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
    url, key, _ = await _conn()
    if not key:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/api/Library/scan-all",
            headers=_headers(key),
            timeout=60,
        )
        resp.raise_for_status()


async def scan_library(library_id: int | None = None) -> None:
    url, key, default_lid = await _conn()
    if not key:
        return
    lid = library_id or default_lid
    if not lid:
        await scan_all_libraries()
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/api/Library/scan",
            headers=_headers(key),
            params={"libraryId": lid},
            timeout=60,
        )
        resp.raise_for_status()


async def search_library(query: str) -> list[dict]:
    """Search Kavita library; returns list of {title, author} for matching."""
    url, key, _ = await _conn()
    if not key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Search/search",
                params={"queryString": query, "includeChapterAndFiles": True},
                headers=_headers(key),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    series_authors: dict[str, str] = {}
    for ch in data.get("chapters", []) or []:
        writers = ch.get("writers") or []
        if writers:
            author_name = writers[0].get("name", "")
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
        for ch_title, ch_author in series_authors.items():
            if ch_title == name or name in ch_title or ch_title in name:
                author = ch_author
                break
        items.append({"title": name, "author": author})
    return items


async def health_check() -> bool:
    url, _, _ = await _conn()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{url}/api/health", timeout=5)
            return resp.status_code == 200
    except Exception:
        return False


async def get_all_series(library_id: int | None = None, formats: list[int] | None = None) -> list[dict]:
    """Fetch all series from Kavita; optionally filter by format (e.g. EBOOK_FORMATS for ebooks)."""
    url, key, default_lid = await _conn()
    if not key:
        return []
    lid = library_id or default_lid
    cache_key = f"kavita_series:{lid}:{','.join(map(str, formats or []))}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{url}/api/Series/all-v2",
                headers={**_headers(key), "Content-Type": "application/json"},
                json={},
                params={"PageSize": 0},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
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
    url, key, _ = await _conn()
    if not key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Series/volumes",
                params={"seriesId": series_id},
                headers=_headers(key),
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
    """Map Kavita library paths to the mounted ebook directory."""
    if not kavita_path:
        return None
    raw = kavita_path.strip()
    as_abs = Path(raw)
    if as_abs.is_file():
        return as_abs

    rel = raw.lstrip("/")
    ebook_root = Path(settings.ebook_dir)

    candidates: list[Path] = []
    parts = rel.split("/", 1)
    if len(parts) == 2 and parts[0].lower() in {
        "manga", "books", "ebooks", "ebook", "library", "comics", "pdf", "pdfs",
    }:
        candidates.append(ebook_root / parts[1])
    elif len(parts) == 2:
        candidates.append(ebook_root / parts[1])
    candidates.append(ebook_root / rel)

    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return candidates[0] if candidates else None


async def get_book_info(chapter_id: int) -> dict | None:
    """Get EPUB/PDF metadata for the reader (caches the file on Kavita)."""
    url, key, _ = await _conn()
    if not key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Book/{chapter_id}/book-info",
                headers=_headers(key),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Failed to fetch Kavita book-info for chapter %s: %s", chapter_id, e)
        return None


async def get_book_chapters(chapter_id: int) -> list[dict]:
    """Get TOC / page mappings for an EPUB chapter."""
    url, key, _ = await _conn()
    if not key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Book/{chapter_id}/chapters",
                headers=_headers(key),
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
    url, key, _ = await _conn()
    if not key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Book/{chapter_id}/book-page",
                params={"page": page},
                headers=_headers(key),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        logger.warning("Failed to fetch Kavita book-page %s: %s", chapter_id, e)
        return None


async def get_book_resources(chapter_id: int, file_path: str) -> bytes | None:
    """Fetch a resource (image, font, etc.) from within an EPUB."""
    url, key, _ = await _conn()
    if not key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Book/{chapter_id}/book-resources",
                params={"file": file_path},
                headers=_headers(key),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.warning("Failed to fetch Kavita resource %s: %s", file_path, e)
        return None
