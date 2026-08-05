import asyncio
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

# Last known-good series list per cache key. Used to reject partial snapshots
# taken while a Kavita scan is rebuilding series/volumes (mid-scan reads made
# the ebook count shrink, then bounce back after the next cache expiry).
_last_good_series: dict[str, list[dict]] = {}
_SHRINK_GUARD_RATIO = 0.7
_SHRINK_GUARD_MIN_PREV = 10

# Last known-good volume list per series id. While a refresh pipeline run is
# active, Kavita rebuilds series/volumes and /api/Series/volumes can briefly
# return fewer volumes (e.g. a 3-volume series collapsing to 1); accepting that
# snapshot made books vanish from the shelf until the next rebuild.
_last_good_vols: dict[int, list[dict]] = {}


def _refresh_pipeline_active() -> bool:
    """True while the serialized ABS+Kavita refresh pipeline is running."""
    try:
        from app.services import library_refresh

        return library_refresh.is_running()
    except Exception:
        return False


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
    try:
        from app.services import library_collection_cache

        library_collection_cache.invalidate()
    except Exception:
        pass


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


async def scan_series(series_id: int, library_id: int | None = None, *, force: bool = True) -> bool:
    """Targeted scan of one series (cheap) — avoids full-library scans after metadata edits."""
    url, key, default_lid = await _conn()
    if not key:
        return False
    lid = library_id or default_lid or 1
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{url}/api/Series/scan",
                headers={**_headers(key), "Content-Type": "application/json"},
                json={"libraryId": lid, "seriesId": series_id, "forceUpdate": force},
                timeout=60,
            )
            resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Kavita series scan for %s failed: %s", series_id, e)
        return False


async def get_library_last_scanned(library_id: int | None = None) -> str | None:
    """Return the library's ``lastScanned`` timestamp (advances when a scan finishes)."""
    url, key, default_lid = await _conn()
    if not key:
        return None
    lid = library_id or default_lid
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{url}/api/Library/libraries",
            headers=_headers(key),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        return None
    for lib in data:
        if not isinstance(lib, dict):
            continue
        if lid is None or lib.get("id") == lid:
            return lib.get("lastScanned")
    return None


async def scan_library_and_wait(
    library_id: int | None = None,
    *,
    timeout_seconds: float = 300,
    poll_interval: float = 5.0,
) -> dict[str, Any]:
    """Trigger a Kavita scan and poll ``lastScanned`` until it advances or timeout.

    The in-process cache is invalidated only AFTER the scan completes so
    collection reads never snapshot Kavita mid-scan (partial series/volumes).
    """
    result: dict[str, Any] = {
        "scan_ran": False,
        "scan_complete": False,
        "timed_out": False,
        "waited_seconds": 0.0,
        "last_scanned": None,
    }
    try:
        before = await get_library_last_scanned(library_id)
    except Exception as e:
        logger.warning("Kavita unreachable before scan: %s", e)
        raise

    started = time.monotonic()
    await scan_library(library_id)
    result["scan_ran"] = True

    after = before
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= timeout_seconds:
            result.update(timed_out=True, waited_seconds=round(elapsed, 2), last_scanned=after)
            logger.warning(
                "Kavita scan wait timed out after %.0fs (lastScanned %s → %s)",
                elapsed, before, after,
            )
            break
        await asyncio.sleep(poll_interval)
        try:
            after = await get_library_last_scanned(library_id)
        except Exception as e:
            logger.debug("Kavita lastScanned poll failed: %s", e)
            continue
        if after is not None and after != before:
            elapsed = time.monotonic() - started
            result.update(scan_complete=True, waited_seconds=round(elapsed, 2), last_scanned=after)
            logger.info(
                "Kavita scan complete in %.1fs (lastScanned %s → %s)", elapsed, before, after
            )
            break

    invalidate_cache()
    return result


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


async def get_all_series(
    library_id: int | None = None,
    formats: list[int] | None = None,
    *,
    force_refresh: bool = False,
) -> list[dict]:
    """Fetch all series from Kavita; optionally filter by format (e.g. EBOOK_FORMATS for ebooks)."""
    url, key, default_lid = await _conn()
    if not key:
        return []
    lid = library_id or default_lid
    cache_key = f"kavita_series:{lid}:{','.join(map(str, formats or []))}"
    if not force_refresh:
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

        # Shrink guard: a scan in progress can return a partial series list.
        # Never let a sharply smaller snapshot replace the last known-good one.
        # While our own refresh pipeline is running, reject ANY shrink — the
        # scan is rebuilding rows and even a small dip is mid-scan noise.
        prev = _last_good_series.get(cache_key)
        if prev is not None and len(items) < len(prev) and _refresh_pipeline_active():
            logger.warning(
                "Kavita series snapshot shrank %d → %d during refresh pipeline — keeping previous",
                len(prev),
                len(items),
            )
            _cache_set(cache_key, prev)
            return prev
        if (
            prev is not None
            and len(prev) >= _SHRINK_GUARD_MIN_PREV
            and len(items) < len(prev) * _SHRINK_GUARD_RATIO
        ):
            logger.warning(
                "Kavita series snapshot shrank %d → %d (scan in progress?) — keeping previous",
                len(prev),
                len(items),
            )
            _cache_set(cache_key, prev)
            return prev

        _last_good_series[cache_key] = items
        _cache_set(cache_key, items)
        return items
    except Exception as e:
        logger.warning("Failed to fetch Kavita series: %s", e)
        prev = _last_good_series.get(cache_key)
        if prev is not None:
            return prev
        return []


async def get_series_volumes(series_id: int) -> list[dict]:
    """Get volumes and chapters for a series (short-TTL cached)."""
    url, key, _ = await _conn()
    if not key:
        return []
    cache_key = f"kavita_vols:{series_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if isinstance(cached, list) else []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Series/volumes",
                params={"seriesId": series_id},
                headers=_headers(key),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            volumes = data if isinstance(data, list) else []

        # Volume shrink guard: mid-scan reads can briefly drop volumes from a
        # series (Kavita rebuilds them row by row). While the refresh pipeline
        # is active, never let a shorter list replace the last known-good one.
        prev = _last_good_vols.get(series_id)
        if prev and len(volumes) < len(prev) and _refresh_pipeline_active():
            logger.warning(
                "Kavita volumes for series %s shrank %d → %d during refresh pipeline — keeping previous",
                series_id,
                len(prev),
                len(volumes),
            )
            _cache_set(cache_key, prev)
            return prev

        _last_good_vols[series_id] = volumes
        _cache_set(cache_key, volumes)
        return volumes
    except Exception as e:
        logger.warning("Failed to fetch Kavita volumes for series %s: %s", series_id, e)
        prev = _last_good_vols.get(series_id)
        return list(prev) if prev is not None else []


async def get_series_metadata(series_id: int) -> dict:
    """Series metadata (genres, tags, writers). Cached briefly with the series list."""
    url, key, _ = await _conn()
    if not key:
        return {}
    cache_key = f"kavita_meta:{series_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{url}/api/Series/metadata",
                params={"seriesId": series_id},
                headers=_headers(key),
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()
            data = raw if isinstance(raw, dict) else {}
        _cache_set(cache_key, data)
        return data
    except Exception as e:
        logger.debug("Failed to fetch Kavita metadata for series %s: %s", series_id, e)
        return {}


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
    # Do not invent non-existent paths — callers treat returns as real files.
    return None


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


async def update_series_identity(
    series_id: int,
    *,
    name: str,
    author: str | None = None,
    summary: str | None = None,
) -> bool:
    """Best-effort Kavita series name + metadata update after OPF embed.

    Kavita re-reads file tags on scan; this also pins UI fields immediately.
    """
    url, key, _ = await _conn()
    if not key:
        return False
    title = (name or "").strip()
    if not title:
        return False
    try:
        from app.utils.book_series import is_corrupt_metadata_value

        if is_corrupt_metadata_value(title):
            logger.warning(
                "Refusing Kavita identity pin for series %s — corrupt name %r",
                series_id,
                title[:80],
            )
            return False
    except Exception:
        pass

    ok_name = False
    ok_meta = False
    try:
        async with httpx.AsyncClient() as client:
            # Fetch current series so we can preserve fields Kavita requires.
            series_payload: dict[str, Any] = {
                "id": series_id,
                "name": title,
                "localizedName": title,
                "sortName": title,
                "sortNameLocked": True,
                "localizedNameLocked": True,
                "nameLocked": True,
            }
            try:
                detail = await client.get(
                    f"{url}/api/Series/{series_id}",
                    headers=_headers(key),
                    timeout=15,
                )
                if detail.status_code == 200:
                    raw = detail.json()
                    if isinstance(raw, dict):
                        series_payload = {**raw, **series_payload}
            except Exception:
                logger.debug("Kavita series detail fetch failed for %s", series_id, exc_info=True)

            resp = await client.post(
                f"{url}/api/Series/update",
                headers={**_headers(key), "Content-Type": "application/json"},
                json=series_payload,
                timeout=30,
            )
            ok_name = resp.status_code in (200, 204)
            if not ok_name:
                logger.warning(
                    "Kavita Series/update for %s failed: HTTP %s %s",
                    series_id,
                    resp.status_code,
                    (resp.text or "")[:200],
                )

            writers: list[dict[str, Any]] = []
            if (author or "").strip():
                writers = [{"id": 0, "name": author.strip()}]
            meta_body: dict[str, Any] = {
                "seriesMetadata": {
                    "seriesId": series_id,
                    "summary": (summary or "").strip(),
                    "writers": writers,
                    "summaryLocked": bool((summary or "").strip()),
                    "writerLocked": bool(writers),
                }
            }
            # Merge existing metadata when available so we do not wipe genres/tags.
            try:
                existing = await get_series_metadata(series_id)
                if existing:
                    merged = {**existing, **meta_body["seriesMetadata"]}
                    merged["seriesId"] = series_id
                    if writers:
                        merged["writers"] = writers
                    if (summary or "").strip():
                        merged["summary"] = summary.strip()
                    meta_body["seriesMetadata"] = merged
            except Exception:
                pass

            meta_resp = await client.post(
                f"{url}/api/Series/metadata",
                headers={**_headers(key), "Content-Type": "application/json"},
                json=meta_body,
                timeout=30,
            )
            ok_meta = meta_resp.status_code in (200, 204)
            if not ok_meta:
                logger.warning(
                    "Kavita Series/metadata for %s failed: HTTP %s %s",
                    series_id,
                    meta_resp.status_code,
                    (meta_resp.text or "")[:200],
                )
    except Exception as e:
        logger.warning("Kavita update_series_identity for %s failed: %s", series_id, e)
        return False

    if ok_name or ok_meta:
        invalidate_cache()
    return ok_name or ok_meta


async def delete_series(series_id: int) -> bool:
    """Delete a series from Kavita."""
    url, key, _ = await _conn()
    if not key:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{url}/api/Series/{series_id}",
                headers=_headers(key),
                timeout=30,
            )
            if resp.status_code in (200, 204):
                invalidate_cache()
                return True
            logger.warning(
                "Kavita delete series %s failed: HTTP %s",
                series_id,
                resp.status_code,
            )
            return False
    except Exception as e:
        logger.warning("Kavita delete series %s failed: %s", series_id, e)
        return False


async def get_series_local_file_paths(series_id: int) -> list[Path]:
    """Resolve all on-disk ebook files for a Kavita series."""
    volumes = await get_series_volumes(series_id)
    paths: list[Path] = []
    seen: set[str] = set()
    for vol in volumes:
        for ch in vol.get("chapters") or []:
            for f in ch.get("files") or []:
                kavita_path = f.get("filePath") or ""
                local = _kavita_path_to_local(kavita_path)
                if not local:
                    continue
                key = str(local.resolve())
                if key in seen:
                    continue
                seen.add(key)
                paths.append(local)
    return paths


async def find_series_id_for_ebook_path(ebook_path: Path | str) -> int | None:
    """Locate the Kavita series id that owns ``ebook_path`` on disk."""
    try:
        target = Path(ebook_path).resolve()
    except OSError:
        return None
    if not target.is_file():
        return None

    folder_key = target.parent.name.casefold()
    series_list = await get_all_series(formats=EBOOK_FORMATS, force_refresh=True)
    if not series_list:
        return None

    def _name_hit(s: dict) -> bool:
        for key in ("name", "localizedName", "originalName", "sortName"):
            val = str(s.get(key) or "").strip().casefold()
            if val and (val == folder_key or folder_key in val or val in folder_key):
                return True
        return False

    ordered = [s for s in series_list if _name_hit(s)]
    ordered.extend(s for s in series_list if s not in ordered)

    for s in ordered:
        sid = s.get("id")
        if sid is None:
            continue
        try:
            sid_i = int(sid)
        except (TypeError, ValueError):
            continue
        try:
            paths = await get_series_local_file_paths(sid_i)
        except Exception:
            continue
        for p in paths:
            try:
                if p.resolve() == target:
                    return sid_i
            except OSError:
                continue
    return None


async def set_series_cover_from_url(series_id: int, cover_url: str) -> bool:
    """Download a cover URL and lock it on the Kavita series via Upload/series.

    Sends raw base64 (no data-URI prefix) — Kavita silently fails when the
    prefix is included.
    """
    import base64

    url = (cover_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    api_url, key, _ = await _conn()
    if not key:
        return False

    try:
        async with httpx.AsyncClient() as client:
            img_resp = await client.get(url, timeout=45, follow_redirects=True)
            img_resp.raise_for_status()
            data = img_resp.content
            if not data or len(data) > 10 * 1024 * 1024:
                return False
            # Strip accidental data-URI prefix if a proxy returned one.
            if data[:5] == b"data:":
                try:
                    data = base64.b64decode(data.split(b",", 1)[1])
                except Exception:
                    return False
            b64 = base64.b64encode(data).decode("ascii")
            resp = await client.post(
                f"{api_url}/api/Upload/series",
                headers={**_headers(key), "Content-Type": "application/json"},
                json={"id": int(series_id), "url": b64, "lockCover": True},
                timeout=60,
            )
            ok = resp.status_code in (200, 204)
            if not ok:
                logger.warning(
                    "Kavita Upload/series cover for %s failed: HTTP %s %s",
                    series_id,
                    resp.status_code,
                    (resp.text or "")[:200],
                )
                return False
            invalidate_cache()
            return True
    except Exception as e:
        logger.warning("Kavita set cover for series %s failed: %s", series_id, e)
        return False
