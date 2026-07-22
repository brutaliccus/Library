import asyncio
import logging
import time
from typing import Any

import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300  # 5 minutes


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = (time.time(), data)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.abs_api_key}"}


async def scan_library(library_id: str | None = None) -> None:
    """Trigger an ABS library scan (fire-and-forget on the ABS side).

    ABS responds HTTP 200 immediately and continues scanning in the background.
    Prefer :func:`scan_library_and_wait` when callers need a complete index.
    """
    lid = library_id or settings.abs_library_id
    if not lid:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.abs_url}/api/libraries/{lid}/scan",
            headers=_headers(),
            timeout=60,
        )
        resp.raise_for_status()


async def get_library(library_id: str | None = None) -> dict[str, Any] | None:
    """Return a single ABS library object (includes ``lastScan`` after a finished scan)."""
    lid = library_id or settings.abs_library_id
    if not lid:
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.abs_url}/api/libraries/{lid}",
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("library"), dict):
            return data["library"]
        return data if isinstance(data, dict) else None


async def get_library_item_total(library_id: str | None = None) -> int | None:
    """Cheap item count via paginated items endpoint (``total`` field)."""
    lid = library_id or settings.abs_library_id
    if not lid:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/api/libraries/{lid}/items",
                params={"limit": "1", "page": "0", "minified": "1"},
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            total = data.get("total")
            return int(total) if total is not None else None
    except Exception as e:
        logger.warning("ABS item total fetch failed: %s", e)
        return None


async def scan_library_and_wait(
    library_id: str | None = None,
    *,
    timeout_seconds: float = 240,
    poll_interval: float = 2.5,
) -> dict[str, Any]:
    """Trigger ABS scan and poll until ``lastScan`` advances or timeout.

    Audiobookshelf's ``POST /api/libraries/:id/scan`` returns 200 immediately while
    ``LibraryScanner`` runs in the background. Completion is observable when the
    library's ``lastScan`` timestamp updates (set only after a non-canceled scan).

    Returns keys: ``scan_ran``, ``scan_complete``, ``timed_out``, ``items_total``,
    ``waited_seconds``, ``last_scan``.
    """
    lid = library_id or settings.abs_library_id
    empty = {
        "scan_ran": False,
        "scan_complete": False,
        "timed_out": False,
        "items_total": None,
        "waited_seconds": 0.0,
        "last_scan": None,
    }
    if not lid:
        return empty

    before_lib = await get_library(lid)
    before_last = (before_lib or {}).get("lastScan")
    started = time.monotonic()

    await scan_library(lid)
    empty["scan_ran"] = True

    # Fallback: if lastScan never moves (already-scanning race, older ABS), treat
    # a stable item total across several polls as "done enough".
    stable_needed = 3
    stable_hits = 0
    last_total: int | None = None
    after_last = before_last
    items_total = await get_library_item_total(lid)

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= timeout_seconds:
            logger.warning(
                "ABS scan wait timed out after %.1fs (library=%s lastScan=%s→%s total=%s)",
                elapsed,
                lid,
                before_last,
                after_last,
                items_total,
            )
            return {
                "scan_ran": True,
                "scan_complete": False,
                "timed_out": True,
                "items_total": items_total,
                "waited_seconds": round(elapsed, 2),
                "last_scan": after_last,
            }

        await asyncio.sleep(poll_interval)

        lib = await get_library(lid)
        after_last = (lib or {}).get("lastScan", after_last)
        items_total = await get_library_item_total(lid)

        if before_last != after_last and after_last is not None:
            # lastScan advances only after LibraryScanner finishes successfully.
            elapsed = time.monotonic() - started
            logger.info(
                "ABS scan complete for %s in %.1fs (lastScan %s → %s, items=%s)",
                lid,
                elapsed,
                before_last,
                after_last,
                items_total,
            )
            invalidate_cache()
            return {
                "scan_ran": True,
                "scan_complete": True,
                "timed_out": False,
                "items_total": items_total,
                "waited_seconds": round(elapsed, 2),
                "last_scan": after_last,
            }

        if items_total is not None:
            if items_total == last_total:
                stable_hits += 1
            else:
                stable_hits = 0
                last_total = items_total
            # Require some wall time so we don't declare "done" on a slow start.
            if stable_hits >= stable_needed and elapsed >= max(8.0, poll_interval * stable_needed):
                elapsed = time.monotonic() - started
                logger.info(
                    "ABS scan inferred complete via stable item total=%s for %s after %.1fs "
                    "(lastScan unchanged at %s)",
                    items_total,
                    lid,
                    elapsed,
                    after_last,
                )
                invalidate_cache()
                return {
                    "scan_ran": True,
                    "scan_complete": True,
                    "timed_out": False,
                    "items_total": items_total,
                    "waited_seconds": round(elapsed, 2),
                    "last_scan": after_last,
                }


async def match_all_items(library_id: str | None = None) -> bool:
    """Trigger ABS to auto-match all unmatched items against metadata providers."""
    lid = library_id or settings.abs_library_id
    if not lid:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/api/libraries/{lid}/matchall",
                headers=_headers(),
                timeout=120,
            )
            resp.raise_for_status()
            logger.info(f"ABS match-all triggered for library {lid}")
            return True
    except Exception as e:
        logger.warning(f"ABS match-all failed: {e}")
        return False


async def remove_items_with_issues(library_id: str | None = None) -> bool:
    """Remove library items whose underlying files are missing (orphaned entries).

    ABS marks items as 'isMissing' when their files no longer exist on disk.
    A library scan detects this, then this endpoint cleans them up.
    """
    lid = library_id or settings.abs_library_id
    if not lid:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{settings.abs_url}/api/libraries/{lid}/issues",
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            logger.info(f"ABS removed items with issues for library {lid}")
            invalidate_cache()
            return True
    except Exception as e:
        logger.warning(f"ABS remove-issues failed: {e}")
        return False


def invalidate_cache() -> None:
    """Clear all ABS caches so next request fetches fresh data."""
    _cache.clear()


async def get_libraries() -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.abs_url}/api/libraries",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("libraries", data) if isinstance(data, dict) else data


async def search_library(query: str) -> list[dict]:
    """Search the configured audiobook library; returns list of {title, author} for matching."""
    if not settings.abs_api_key or not settings.abs_library_id:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/api/libraries/{settings.abs_library_id}/search",
                params={"q": query},
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []
    items: list[dict] = []
    for key in ("book", "podcast"):
        for entry in data.get(key, []):
            lib_item = entry.get("libraryItem", {})
            media = lib_item.get("media", {})
            meta = media.get("metadata", {})
            title = meta.get("title") or meta.get("titleIgnorePrefix") or ""
            author = meta.get("authorName") or ""
            if title:
                items.append({"title": title, "author": author})
    return items


async def get_library_item(item_id: str) -> dict | None:
    """Fetch a single library item with full metadata and audio tracks."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/api/items/{item_id}",
                params={"expanded": "1"},
                headers=_headers(),
                timeout=15,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


def chapters_from_library_item(lib_item: dict | None) -> list[dict]:
    """Chapter markers from ABS library item media (times are seconds from book start)."""
    if not lib_item:
        return []
    media = lib_item.get("media") or {}
    raw = media.get("chapters")
    if not raw or not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, ch in enumerate(raw):
        if not isinstance(ch, dict):
            continue
        try:
            start = float(ch.get("start", 0))
        except (TypeError, ValueError):
            start = 0.0
        end_val = ch.get("end")
        try:
            end = float(end_val) if end_val is not None else None
        except (TypeError, ValueError):
            end = None
        title = (ch.get("title") or "").strip() or f"Chapter {i + 1}"
        cid = ch.get("id")
        try:
            cid = int(cid) if cid is not None else i
        except (TypeError, ValueError):
            cid = i
        out.append({"id": cid, "title": title, "start": start, "end": end})
    out.sort(key=lambda c: c["start"])
    return out


async def get_item_chapters(item_id: str) -> list[dict] | None:
    """Return normalized chapters for a library item, or None if the item is missing."""
    item = await get_library_item(item_id)
    if item is None:
        return None
    return chapters_from_library_item(item)


def first_audio_file_id(lib_item: dict | None) -> str | None:
    """Inode/file id for the first audio file on a library item (for warmup reads)."""
    if not lib_item:
        return None
    media = lib_item.get("media") or {}
    for key in ("tracks", "audioFiles"):
        for f in media.get(key) or []:
            if not isinstance(f, dict):
                continue
            ino = f.get("ino") or f.get("inode")
            if ino is not None and str(ino).strip():
                return str(ino).strip()
    return None


async def warmup_item_playback(item_id: str) -> bool:
    """Read the first ~256KB of the first audio file so spinning disks / ABS can serve playback sooner."""
    item = await get_library_item(item_id)
    file_id = first_audio_file_id(item)
    if not file_id:
        return False
    url = f"{settings.abs_url}/api/items/{item_id}/file/{file_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={**_headers(), "Range": "bytes=0-262143"},
                timeout=45,
            )
            return resp.status_code in (200, 206)
    except Exception as e:
        logger.debug("ABS warmup for %s failed: %s", item_id, e)
        return False


async def start_playback_session(item_id: str) -> dict | None:
    """Start (or resume) a playback session. Returns session info with audio tracks."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.abs_url}/api/items/{item_id}/play",
                headers=_headers(),
                json={
                    "deviceInfo": {
                        "clientName": "LibrarySite",
                        "deviceId": "library-site-player",
                    },
                    "forceDirectPlay": True,
                    "forceTranscode": False,
                },
                timeout=45,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


async def sync_session(session_id: str, current_time: float, duration: float) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.abs_url}/api/session/{session_id}/sync",
                headers=_headers(),
                json={"currentTime": current_time, "duration": duration, "timeListened": 0},
                timeout=10,
            )
            return resp.status_code in (200, 204)
    except Exception:
        return False


async def close_session(session_id: str, current_time: float, duration: float) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.abs_url}/api/session/{session_id}/close",
                headers=_headers(),
                json={"currentTime": current_time, "duration": duration, "timeListened": 0},
                timeout=10,
            )
            return resp.status_code in (200, 204)
    except Exception:
        return False


async def reset_item_progress(item_id: str) -> bool:
    """Clear saved listening progress for a library item (shared ABS account)."""
    url = f"{settings.abs_url}/api/me/progress/{item_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(url, headers=_headers(), timeout=15)
            if resp.status_code in (200, 204, 404):
                return True
            # Some ABS versions expect a PATCH with zeroed progress instead of DELETE
            resp = await client.patch(
                url,
                headers=_headers(),
                json={"currentTime": 0, "progress": 0, "isFinished": False},
                timeout=15,
            )
            return resp.status_code in (200, 204)
    except Exception as e:
        logger.warning("ABS reset progress for %s failed: %s", item_id, e)
        return False


async def get_items_in_progress() -> list[dict]:
    """Return the user's currently-in-progress listening items."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/api/me/items-in-progress",
                headers=_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("libraryItems", [])
    except Exception:
        return []


async def search_library_with_ids(query: str) -> list[dict]:
    """Like search_library but also returns ABS item IDs for streaming."""
    if not settings.abs_api_key or not settings.abs_library_id:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/api/libraries/{settings.abs_library_id}/search",
                params={"q": query},
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []
    items: list[dict] = []
    for key in ("book", "podcast"):
        for entry in data.get(key, []):
            lib_item = entry.get("libraryItem", {})
            media = lib_item.get("media", {})
            meta = media.get("metadata", {})
            title = meta.get("title") or meta.get("titleIgnorePrefix") or ""
            author = meta.get("authorName") or ""
            cover = lib_item.get("media", {}).get("coverPath") or ""
            item_id = lib_item.get("id") or ""
            if title:
                items.append({
                    "title": title,
                    "author": author,
                    "itemId": item_id,
                    "coverUrl": f"/api/stream/abs/proxy/cover/{item_id}" if item_id else "",
                })
    return items


async def health_check() -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/healthcheck",
                timeout=5,
            )
            return resp.status_code == 200
    except Exception:
        return False


def _normalize_abs_item(lib_item: dict, progress_map: dict | None = None) -> dict:
    """Normalize a raw ABS library item into a consistent dict."""
    media = lib_item.get("media", {})
    meta = media.get("metadata", {})
    item_id = lib_item.get("id", "")
    title = meta.get("title") or meta.get("titleIgnorePrefix") or ""
    author = meta.get("authorName") or ""
    genres = meta.get("genres", [])
    narrator = meta.get("narratorName") or ""
    series_list = meta.get("series", [])
    if isinstance(series_list, dict):
        series_list = [series_list]
    series_info = []
    for s in series_list:
        series_info.append({
            "id": s.get("id", ""),
            "name": s.get("name", ""),
            "sequence": s.get("sequence", ""),
        })
    duration = media.get("duration", 0) or 0
    progress = 0.0
    is_finished = False
    if progress_map and item_id in progress_map:
        mp = progress_map[item_id]
        progress = mp.get("currentTime", 0) / duration if duration else 0
        is_finished = mp.get("isFinished", False)
    return {
        "itemId": item_id,
        "title": title,
        "author": author,
        "narrator": narrator,
        "coverUrl": f"/api/stream/abs/proxy/cover/{item_id}" if item_id else "",
        "genres": genres,
        "series": series_info,
        "duration": round(duration),
        "progress": round(progress, 3),
        "isFinished": is_finished,
        "numTracks": media.get("numTracks", 0) or media.get("numAudioFiles", 0) or 0,
        "addedAt": lib_item.get("addedAt", 0),
    }


async def _fetch_library_items_all_pages(library_id: str) -> list[dict]:
    """Fetch every library item with full media metadata.

    Uses paginated requests. Some Audiobookshelf versions reject or time out on ``limit=0``;
    un-paginated giant responses can also exceed client timeouts.
    """
    all_results: list[dict] = []
    page = 0
    page_size = 400
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                f"{settings.abs_url}/api/libraries/{library_id}/items",
                params={
                    "limit": str(page_size),
                    "page": str(page),
                    "minified": "0",
                    "sort": "media.metadata.title",
                    "collapseseries": "0",
                },
                headers=_headers(),
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("results") or []
            if not batch:
                break
            all_results.extend(batch)
            total = data.get("total")
            if total is not None and len(all_results) >= int(total):
                break
            if len(batch) < page_size:
                break
            page += 1
            if page > 500:
                logger.warning("ABS library items pagination stopped at safety cap (page>500)")
                break
    return all_results


async def get_all_items(library_id: str | None = None) -> list[dict]:
    """Fetch all items from ABS library with metadata (cached)."""
    lid = library_id or settings.abs_library_id
    if not lid or not settings.abs_api_key:
        return []
    cache_key = f"abs_all_items:{lid}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        results = await _fetch_library_items_all_pages(lid)
    except Exception:
        logger.warning("Failed to fetch ABS items", exc_info=True)
        return []

    progress_map = await _get_progress_map()
    items = [_normalize_abs_item(r, progress_map) for r in results if r.get("media", {}).get("metadata", {}).get("title")]
    _cache_set(cache_key, items)
    return items


async def get_all_series(library_id: str | None = None) -> list[dict]:
    """Fetch all series from ABS library (cached)."""
    lid = library_id or settings.abs_library_id
    if not lid or not settings.abs_api_key:
        return []
    cache_key = f"abs_all_series:{lid}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.abs_url}/api/libraries/{lid}/series",
                params={"limit": "500", "minified": "0"},
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.warning("Failed to fetch ABS series")
        return []

    progress_map = await _get_progress_map()
    series_list = []
    for s in data.get("results", []):
        books = []
        total_dur = 0
        for book in s.get("books", []):
            nb = _normalize_abs_item(book, progress_map)
            seq = ""
            for si in nb["series"]:
                if si.get("name") == s.get("name"):
                    seq = si.get("sequence", "")
                    break
            nb["sequence"] = seq
            books.append(nb)
            total_dur += nb["duration"]
        try:
            books.sort(key=lambda b: float(b.get("sequence") or "999"))
        except (ValueError, TypeError):
            books.sort(key=lambda b: b.get("sequence", ""))
        series_list.append({
            "id": s.get("id", ""),
            "name": s.get("name", ""),
            "books": books,
            "bookCount": len(books),
            "totalDuration": round(total_dur),
            "coverUrl": books[0]["coverUrl"] if books else "",
        })
    _cache_set(cache_key, series_list)
    return series_list


async def match_item(item_id: str) -> dict | None:
    """Trigger ABS quick match for a single library item."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.abs_url}/api/items/{item_id}/match",
                headers=_headers(),
                json={"provider": "audible"},
                timeout=30,
            )
            resp.raise_for_status()
            invalidate_cache()
            return resp.json()
    except Exception as e:
        logger.warning(f"ABS match item {item_id} failed: {e}")
        return None


async def update_item_metadata(item_id: str, title: str) -> bool:
    """Update a single ABS item's title via PATCH /api/items/{id}/media."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{settings.abs_url}/api/items/{item_id}/media",
                headers=_headers(),
                json={"metadata": {"title": title}},
                timeout=15,
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning(f"ABS update metadata for {item_id} failed: {e}")
        return False


async def fix_metadata_mismatches(library_id: str | None = None) -> dict[str, Any]:
    """Scan ABS (wait for completion), remove missing-file orphans, align titles with folders.

    Duplicate \"covers\" after a folder layout change are usually orphaned library rows whose
    paths no longer exist; a library scan plus ``DELETE /libraries/{{id}}/issues`` clears those.
    Title/folder mismatches are a separate class of problem this pass also fixes.

    Returns keys: ``fixed``, ``count``, ``scan_ran``, ``scan_complete``, ``timed_out``,
    ``waited_seconds``, ``items_total``, ``orphan_cleanup_ok``, ``items_examined``,
    ``fetch_error`` (set when the item list could not be loaded).
    """
    lid = library_id or settings.abs_library_id
    empty = {
        "fixed": [],
        "count": 0,
        "scan_ran": False,
        "scan_complete": False,
        "timed_out": False,
        "waited_seconds": 0.0,
        "items_total": None,
        "orphan_cleanup_ok": False,
        "items_examined": 0,
        "fetch_error": None,
    }
    if not lid or not settings.abs_api_key:
        empty["fetch_error"] = "Audiobookshelf library is not configured"
        return empty

    scan_ran = False
    scan_complete = False
    timed_out = False
    waited_seconds = 0.0
    items_total: int | None = None
    orphan_cleanup_ok = False
    try:
        scan_status = await scan_library_and_wait(lid)
        scan_ran = bool(scan_status.get("scan_ran"))
        scan_complete = bool(scan_status.get("scan_complete"))
        timed_out = bool(scan_status.get("timed_out"))
        waited_seconds = float(scan_status.get("waited_seconds") or 0)
        items_total = scan_status.get("items_total")
        orphan_cleanup_ok = await remove_items_with_issues(lid)
    except Exception as e:
        logger.warning("fix_metadata_mismatches: library scan / orphan cleanup failed: %s", e)

    try:
        items = await _fetch_library_items_all_pages(lid)
    except Exception as e:
        logger.warning("Failed to paginate-fetch ABS items for mismatch fix: %s", e, exc_info=True)
        out = {
            **empty,
            "scan_ran": scan_ran,
            "scan_complete": scan_complete,
            "timed_out": timed_out,
            "waited_seconds": waited_seconds,
            "items_total": items_total,
            "orphan_cleanup_ok": orphan_cleanup_ok,
        }
        out["fetch_error"] = str(e)
        return out

    fixed: list[dict] = []
    for item in items:
        rel_path = item.get("relPath", "")
        if not rel_path:
            continue
        folder_name = rel_path.rstrip("/").rsplit("/", 1)[-1]
        if not folder_name:
            continue
        media = item.get("media", {})
        meta = media.get("metadata", {})
        current_title = meta.get("title", "")
        if current_title and current_title.strip().lower() != folder_name.strip().lower():
            item_id = item.get("id", "")
            if item_id and await update_item_metadata(item_id, folder_name):
                fixed.append({
                    "itemId": item_id,
                    "oldTitle": current_title,
                    "newTitle": folder_name,
                })
                logger.info("Fixed ABS title: '%s' -> '%s' (%s)", current_title, folder_name, item_id)

    if fixed or scan_ran or orphan_cleanup_ok:
        invalidate_cache()

    return {
        "fixed": fixed,
        "count": len(fixed),
        "scan_ran": scan_ran,
        "scan_complete": scan_complete,
        "timed_out": timed_out,
        "waited_seconds": waited_seconds,
        "items_total": items_total if items_total is not None else len(items),
        "orphan_cleanup_ok": orphan_cleanup_ok,
        "items_examined": len(items),
        "fetch_error": None,
    }


async def _get_progress_map() -> dict:
    """Build a map of itemId -> progress info from in-progress items."""
    try:
        items = await get_items_in_progress()
        return {
            item.get("id", ""): {
                "currentTime": item.get("progressPercent", 0),
                "isFinished": item.get("isFinished", False),
            }
            for item in items
        }
    except Exception:
        return {}
