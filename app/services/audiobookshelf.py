import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from app.config import get_settings
from app.utils.book_series import parse_abs_series_label

logger = logging.getLogger(__name__)
settings = get_settings()

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300  # 5 minutes
# Coalesce concurrent scan_library_and_wait callers onto one in-flight ABS scan.
_scan_lock = asyncio.Lock()
_scan_result_fut: asyncio.Future | None = None
# Prevent stacked POST /scan while ABS is still working (re-POST cancels & restarts → Pi OOM).
_scan_posted_at: float | None = None
_scan_posted_before_last: Any = None
_SCAN_POST_COOLDOWN_SEC = 300.0
# Single background scan+orphan-cleanup task for deferred kicks.
_bg_scan_cleanup_task: asyncio.Task | None = None

# ABS iterates metadataPrecedence low→high; last entry wins. Keep absMetadata highest
# so LibraForge / manual ABS edits are not replaced by folder names on scan.
ABS_METADATA_PRECEDENCE = [
    "folderStructure",
    "audioMetatags",
    "nfoFile",
    "txtFiles",
    "opfFile",
    "absMetadata",
]


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


async def scan_library(library_id: str | None = None, *, force: bool = False) -> bool:
    """Trigger an ABS library scan (fire-and-forget on the ABS side).

    ABS responds HTTP 200 immediately and continues scanning in the background.
    Prefer :func:`scan_library_and_wait` when callers need a complete index.

    Returns True when a new ``POST …/scan`` was sent. Returns False when skipped
    because a prior kick is still in flight (same ``lastScan`` within cooldown) —
    stacking scans on a Pi cancels/restarts LibraryScanner and can OOM the host.
    """
    global _scan_posted_at, _scan_posted_before_last
    lid = library_id or settings.abs_library_id
    if not lid:
        return False

    lib: dict[str, Any] | None = None
    try:
        lib = await get_library(lid)
    except Exception as e:
        logger.warning("ABS unreachable before scan: %s", e)
        raise

    current_last = (lib or {}).get("lastScan")
    now = time.monotonic()
    if (
        not force
        and _scan_posted_at is not None
        and (now - _scan_posted_at) < _SCAN_POST_COOLDOWN_SEC
        and current_last == _scan_posted_before_last
    ):
        logger.info(
            "Skipping ABS scan POST for %s — prior scan still in flight "
            "(lastScan=%s, %.0fs cooldown)",
            lid,
            current_last,
            _SCAN_POST_COOLDOWN_SEC,
        )
        return False

    _scan_posted_before_last = current_last
    _scan_posted_at = now
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.abs_url}/api/libraries/{lid}/scan",
            headers=_headers(),
            # Never force=1 — full rescan of every file melts low-RAM hosts.
            timeout=60,
        )
        resp.raise_for_status()
    return True


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

    Concurrent callers share one in-flight wait (no stacked ``POST …/scan`` on Pi).

    Returns keys: ``scan_ran``, ``scan_complete``, ``timed_out``, ``items_total``,
    ``waited_seconds``, ``last_scan``, ``coalesced``.
    """
    global _scan_result_fut
    lid = library_id or settings.abs_library_id
    empty = {
        "scan_ran": False,
        "scan_complete": False,
        "timed_out": False,
        "items_total": None,
        "waited_seconds": 0.0,
        "last_scan": None,
        "coalesced": False,
    }
    if not lid:
        return empty

    async with _scan_lock:
        if _scan_result_fut is not None and not _scan_result_fut.done():
            fut = _scan_result_fut
            join = True
        else:
            fut = asyncio.get_running_loop().create_future()
            _scan_result_fut = fut
            join = False

    if join:
        result = await fut
        if isinstance(result, dict):
            return {**result, "coalesced": True}
        return result

    try:
        before_lib = await get_library(lid)
        before_last = (before_lib or {}).get("lastScan")
        started = time.monotonic()

        posted = await scan_library(lid)
        if not posted:
            logger.info(
                "ABS scan wait joining in-flight scan for %s (no new POST)",
                lid,
            )

        # Fallback: if lastScan never moves (already-scanning race, older ABS), treat
        # a stable item total across several polls as "done enough".
        stable_needed = 3
        stable_hits = 0
        last_total: int | None = None
        after_last = before_last
        items_total = await get_library_item_total(lid)
        out: dict[str, Any] | None = None

        while out is None:
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
                out = {
                    "scan_ran": True,
                    "scan_complete": False,
                    "timed_out": True,
                    "items_total": items_total,
                    "waited_seconds": round(elapsed, 2),
                    "last_scan": after_last,
                    "coalesced": False,
                }
                break

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
                out = {
                    "scan_ran": True,
                    "scan_complete": True,
                    "timed_out": False,
                    "items_total": items_total,
                    "waited_seconds": round(elapsed, 2),
                    "last_scan": after_last,
                    "coalesced": False,
                }
                break

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
                    out = {
                        "scan_ran": True,
                        "scan_complete": True,
                        "timed_out": False,
                        "items_total": items_total,
                        "waited_seconds": round(elapsed, 2),
                        "last_scan": after_last,
                        "coalesced": False,
                    }

        assert out is not None
        if not fut.done():
            fut.set_result(out)
        return out
    except BaseException as e:
        if not fut.done():
            fut.set_exception(e)
        raise


async def _bg_scan_and_cleanup() -> None:
    """Background: wait for ABS scan, remove orphans, invalidate caches."""
    try:
        await scan_library_and_wait()
        await remove_items_with_issues()
        invalidate_cache()
    except Exception:
        logger.exception("Background ABS scan/cleanup failed")


async def kick_library_scan(*, wait: bool = True) -> dict[str, Any]:
    """Safe ABS scan + orphan cleanup entry point for admin / My Library refresh.

    ``wait=False`` coalesces onto one background task (no stacked scans, no
    blocking the HTTP worker for minutes). Health-checks ABS before kicking.
    """
    global _bg_scan_cleanup_task
    empty = {
        "ok": False,
        "scan_ran": False,
        "scan_complete": False,
        "timed_out": False,
        "deferred": False,
        "already_running": False,
        "waited_seconds": 0.0,
        "items_total": None,
        "error": None,
        "message": "Audiobookshelf library is not configured",
    }
    if not settings.abs_library_id or not settings.abs_api_key:
        return empty

    try:
        healthy = await health_check()
    except Exception as e:
        return {**empty, "error": str(e), "message": "ABS health check failed"}
    if not healthy:
        return {
            **empty,
            "error": "ABS healthcheck failed",
            "message": "Audiobookshelf is not reachable — scan skipped",
        }

    if not wait:
        if _bg_scan_cleanup_task is not None and not _bg_scan_cleanup_task.done():
            return {
                "ok": True,
                "scan_ran": True,
                "scan_complete": False,
                "timed_out": False,
                "deferred": True,
                "already_running": True,
                "waited_seconds": 0.0,
                "items_total": None,
                "error": None,
                "message": "Library scan already running in background",
            }
        _bg_scan_cleanup_task = asyncio.create_task(_bg_scan_and_cleanup())
        return {
            "ok": True,
            "scan_ran": True,
            "scan_complete": False,
            "timed_out": False,
            "deferred": True,
            "already_running": False,
            "waited_seconds": 0.0,
            "items_total": None,
            "error": None,
            "message": "Library scan started; cleanup continues in background",
        }

    try:
        scan_status = await scan_library_and_wait()
        await remove_items_with_issues()
        invalidate_cache()
        complete = bool(scan_status.get("scan_complete"))
        return {
            "ok": True,
            "scan_ran": bool(scan_status.get("scan_ran")),
            "scan_complete": complete,
            "timed_out": bool(scan_status.get("timed_out")),
            "deferred": False,
            "already_running": False,
            "waited_seconds": scan_status.get("waited_seconds") or 0.0,
            "items_total": scan_status.get("items_total"),
            "error": None,
            "message": (
                "Library scanned and cleaned up"
                if complete
                else "Library scan started but did not finish before timeout; refresh again shortly"
            ),
        }
    except Exception as e:
        logger.warning("kick_library_scan wait path failed: %s", e)
        return {
            **empty,
            "error": str(e),
            "message": f"ABS scan failed: {e}",
        }


async def match_all_items(library_id: str | None = None) -> bool:
    """Trigger ABS match-all (provider fetch). Not used by admin scan or forge finalize.

    Prefer :func:`ensure_metadata_hardening` + per-item rematch. Match-all can still
    fill empty fields on items without ASIN; items with ASIN are skipped when
    ``skipMatchingMediaWithAsin`` is enabled on the library.
    """
    lid = library_id or settings.abs_library_id
    if not lid:
        return False
    logger.warning(
        "ABS match-all requested for library %s — not part of admin scan or forge finalize",
        lid,
    )
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
    try:
        from app.services import library_collection_cache

        library_collection_cache.invalidate()
    except Exception:
        pass


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


async def delete_library_item(item_id: str, *, hard: bool = False) -> bool:
    """Remove a library item from Audiobookshelf (soft delete by default)."""
    try:
        params: dict[str, str] = {}
        if hard:
            params["hard"] = "1"
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{settings.abs_url}/api/items/{item_id}",
                params=params or None,
                headers=_headers(),
                timeout=30,
            )
            if resp.status_code in (200, 204):
                invalidate_cache()
                return True
            logger.warning(
                "ABS delete item %s failed: HTTP %s",
                item_id,
                resp.status_code,
            )
            return False
    except Exception as e:
        logger.warning("ABS delete item %s failed: %s", item_id, e)
        return False




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


def offline_download_info_from_item(lib_item: dict | None) -> dict | None:
    """Build proxied track URLs for offline download without starting an ABS play session.

    Uses expanded library-item audio metadata (tracks / audioFiles) so Save offline
    works even if the user has never pressed Listen.
    """
    if not lib_item:
        return None
    item_id = str(lib_item.get("id") or "").strip()
    if not item_id:
        return None
    media = lib_item.get("media") or {}
    meta = media.get("metadata") or {}
    raw = media.get("tracks") or []
    if not raw:
        raw = media.get("audioFiles") or []

    tracks: list[dict] = []
    offset = 0.0
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        ino = entry.get("ino") or entry.get("inode")
        if ino is None or not str(ino).strip():
            continue
        file_id = str(ino).strip()
        try:
            dur = float(entry.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        start_raw = entry.get("startOffset")
        try:
            start = float(start_raw) if start_raw is not None else offset
        except (TypeError, ValueError):
            start = offset
        file_meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        title = (
            (entry.get("title") or "").strip()
            or (file_meta.get("filename") or "").strip()
            or (file_meta.get("title") or "").strip()
            or f"Track {i + 1}"
        )
        tracks.append({
            "index": entry.get("index", i),
            "startOffset": start,
            "duration": dur,
            "title": title,
            "contentUrl": f"/api/stream/abs/proxy/audio/{item_id}/{file_id}",
            "mimeType": entry.get("mimeType") or "audio/mpeg",
        })
        offset = start + dur

    if not tracks:
        return None

    total_duration = media.get("duration") or offset
    try:
        total_duration = float(total_duration or 0)
    except (TypeError, ValueError):
        total_duration = offset

    return {
        "sessionId": "",
        "tracks": tracks,
        "startOffset": 0.0,
        "coverUrl": f"/api/stream/abs/proxy/cover/{item_id}",
        "title": meta.get("title") or lib_item.get("title") or "Audiobook",
        "author": meta.get("authorName") or "",
        "duration": total_duration,
        "chapters": chapters_from_library_item(lib_item),
    }


async def get_offline_download_info(item_id: str) -> dict | None:
    """Fetch ABS item metadata and return offline-download track info (no play session)."""
    item = await get_library_item(item_id)
    return offline_download_info_from_item(item)


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
    """Normalize a raw ABS library item into a consistent dict.

    Series source of truth: ``metadata.seriesName`` (LibraForge / embedded file
    metadata) wins over ABS ``series[]`` relations. ABS can link Anne Rice books
    to the wrong shared series graph (e.g. Mayfair Witches) even when the file
    correctly says Vampire Chronicles.
    """
    media = lib_item.get("media", {})
    meta = media.get("metadata", {})
    item_id = lib_item.get("id", "")
    title = meta.get("title") or meta.get("titleIgnorePrefix") or ""
    author = meta.get("authorName") or ""
    genres = meta.get("genres", [])
    narrator = meta.get("narratorName") or ""
    subtitle = str(meta.get("subtitle") or "").strip()
    asin = str(meta.get("asin") or "").strip()
    description = str(meta.get("description") or meta.get("summary") or "").strip()
    series_list = meta.get("series") or []
    if isinstance(series_list, dict):
        series_list = [series_list]
    series_info = []
    for s in series_list:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or "").strip()
        seq = str(s.get("sequence") or "").strip()
        if not name:
            continue
        # Some ABS matches store "Series #1" in series[].name too.
        parsed_name, parsed_seq = parse_abs_series_label(name)
        if parsed_name:
            name, seq = parsed_name, seq or parsed_seq
        series_info.append({
            "id": s.get("id", ""),
            "name": name,
            "sequence": seq,
        })
    # Prefer embedded/LibraForge seriesName over ABS series[] relations.
    series_name, sequence = parse_abs_series_label(meta.get("seriesName"))
    if series_name:
        # Keep ABS series id when the relation name matches the file label.
        matched_id = ""
        for s in series_info:
            if (s.get("name") or "").strip().lower() == series_name.lower():
                matched_id = s.get("id") or ""
                sequence = sequence or str(s.get("sequence") or "").strip()
                break
        series_info = [{"id": matched_id, "name": series_name, "sequence": sequence}]
    elif series_info:
        series_name = series_info[0]["name"]
        sequence = str(series_info[0].get("sequence") or "")
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
        "subtitle": subtitle,
        "author": author,
        "narrator": narrator,
        "asin": asin,
        "description": description,
        "coverUrl": f"/api/stream/abs/proxy/cover/{item_id}" if item_id else "",
        "genres": genres,
        "series": series_info,
        "seriesName": series_name,
        "sequence": sequence,
        "duration": round(duration),
        "progress": round(progress, 3),
        "isFinished": is_finished,
        "numTracks": media.get("numTracks", 0) or media.get("numAudioFiles", 0) or 0,
        "addedAt": lib_item.get("addedAt", 0),
        # Used by shelf dedupe (chapter-folder fragments / path twins).
        "path": str(lib_item.get("path") or ""),
        "relPath": str(lib_item.get("relPath") or ""),
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


def peek_cached_all_items(library_id: str | None = None) -> list[dict] | None:
    """Return ABS all-items cache if warm; never triggers a network fetch."""
    lid = library_id or settings.abs_library_id
    if not lid:
        return None
    cached = _cache_get(f"abs_all_items:{lid}")
    return cached if isinstance(cached, list) else None


async def get_all_items(
    library_id: str | None = None,
    *,
    force_refresh: bool = False,
) -> list[dict]:
    """Fetch all items from ABS library with metadata (cached)."""
    lid = library_id or settings.abs_library_id
    if not lid or not settings.abs_api_key:
        return []
    cache_key = f"abs_all_items:{lid}"
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        results = await _fetch_library_items_all_pages(lid)
    except Exception:
        logger.warning("Failed to fetch ABS items", exc_info=True)
        return []

    progress_map = await _get_progress_map()
    items = []
    for r in results:
        # Drop ghost rows after folder moves/consolidations (isMissing) so the
        # shelf does not inflate (e.g. 26 deleted Red Seas chapter folders).
        if r.get("isMissing") or r.get("isInvalid"):
            continue
        title = (r.get("media") or {}).get("metadata", {}).get("title")
        if not title:
            continue
        items.append(_normalize_abs_item(r, progress_map))
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


async def match_item(
    item_id: str,
    *,
    override_defaults: bool = False,
    force: bool = False,
) -> dict | None:
    """Trigger ABS quick match for a single library item (Audible provider).

    By default does **not** force-overwrite existing fields (``overrideDefaults=false``).
    Books that already have an ASIN are skipped unless ``force=True`` — LibraForge /
    manual metadata should win over another provider round-trip.
    """
    if not force:
        item = await get_library_item(item_id)
        meta = ((item or {}).get("media") or {}).get("metadata") or {}
        asin = str(meta.get("asin") or "").strip()
        if asin:
            logger.info(
                "Skipping ABS Quick Match for %s — ASIN already set (%s)",
                item_id,
                asin,
            )
            return {
                "skipped": True,
                "reason": "asin_present",
                "asin": asin,
                "updated": False,
            }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.abs_url}/api/items/{item_id}/match",
                headers=_headers(),
                json={
                    "provider": "audible",
                    "overrideDefaults": bool(override_defaults),
                },
                timeout=30,
            )
            resp.raise_for_status()
            invalidate_cache()
            return resp.json()
    except Exception as e:
        logger.warning(f"ABS match item {item_id} failed: {e}")
        return None


async def update_item_metadata(item_id: str, title: str | None = None, *, metadata: dict | None = None) -> bool:
    """Update ABS item media metadata via PATCH /api/items/{id}/media.

    Pass ``title`` for a title-only update, or a full ``metadata`` dict (LibraForge sync).
    """
    payload_meta: dict[str, Any] = dict(metadata or {})
    if title is not None:
        payload_meta["title"] = title
    if not payload_meta:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{settings.abs_url}/api/items/{item_id}/media",
                headers=_headers(),
                json={"metadata": payload_meta},
                timeout=30,
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning(f"ABS update metadata for {item_id} failed: {e}")
        return False


async def update_library_settings(
    library_id: str | None = None,
    *,
    settings_patch: dict[str, Any] | None = None,
    provider: str | None = None,
) -> dict[str, Any] | None:
    """PATCH ABS library settings (e.g. skipMatchingMediaWithAsin)."""
    lid = library_id or settings.abs_library_id
    if not lid or not settings.abs_api_key:
        return None
    body: dict[str, Any] = {}
    if settings_patch:
        body["settings"] = settings_patch
    if provider is not None:
        body["provider"] = provider
    if not body:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{settings.abs_url}/api/libraries/{lid}",
                headers=_headers(),
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("library") if isinstance(data, dict) and "library" in data else data
    except Exception as e:
        logger.warning("ABS update library settings failed: %s", e)
        return None


async def update_server_settings(settings_patch: dict[str, Any]) -> dict[str, Any] | None:
    """PATCH ABS server settings (e.g. scannerPreferMatchedMetadata)."""
    if not settings.abs_api_key or not settings_patch:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{settings.abs_url}/api/settings",
                headers=_headers(),
                json=settings_patch,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("serverSettings") if isinstance(data, dict) else data
    except Exception as e:
        logger.warning("ABS update server settings failed: %s", e)
        return None


async def ensure_metadata_hardening(library_id: str | None = None) -> dict[str, Any]:
    """Pin ABS settings that protect LibraForge / manual metadata from rematch drift.

    - Library: skip match when ASIN/ISBN present; absMetadata highest precedence
    - Server: Quick Match must not force-overwrite (scannerPreferMatchedMetadata=false)
    """
    out: dict[str, Any] = {
        "library_ok": False,
        "server_ok": False,
        "skip_asin": None,
        "prefer_matched": None,
        "precedence": None,
    }
    lib = await update_library_settings(
        library_id,
        settings_patch={
            "skipMatchingMediaWithAsin": True,
            "skipMatchingMediaWithIsbn": True,
            "metadataPrecedence": list(ABS_METADATA_PRECEDENCE),
        },
    )
    if lib:
        lib_settings = lib.get("settings") if isinstance(lib, dict) else None
        if isinstance(lib_settings, dict):
            out["skip_asin"] = lib_settings.get("skipMatchingMediaWithAsin")
            out["precedence"] = lib_settings.get("metadataPrecedence")
        out["library_ok"] = True

    server = await update_server_settings({"scannerPreferMatchedMetadata": False})
    if server:
        out["prefer_matched"] = server.get("scannerPreferMatchedMetadata")
        out["server_ok"] = True

    if out["library_ok"] or out["server_ok"]:
        logger.info(
            "ABS metadata hardening applied (library_ok=%s server_ok=%s skip_asin=%s "
            "prefer_matched=%s)",
            out["library_ok"],
            out["server_ok"],
            out["skip_asin"],
            out["prefer_matched"],
        )
    return out


async def find_item_by_rel_path(rel_path: str, library_id: str | None = None) -> dict | None:
    """Return the ABS library item whose ``relPath`` matches (case-insensitive)."""
    needle = (rel_path or "").strip().strip("/").replace("\\", "/")
    if not needle:
        return None
    lid = library_id or settings.abs_library_id
    try:
        items = await _fetch_library_items_all_pages(lid) if lid else []
    except Exception as e:
        logger.warning("ABS find_item_by_rel_path fetch failed: %s", e)
        return None
    needle_l = needle.lower()
    for item in items:
        rel = str(item.get("relPath") or "").strip().strip("/").replace("\\", "/")
        if rel.lower() == needle_l:
            return item
    # Fallback: suffix match when ABS nests deeper than our target folder.
    for item in items:
        rel = str(item.get("relPath") or "").strip().strip("/").replace("\\", "/")
        if rel.lower().endswith("/" + needle_l) or rel.lower().endswith(needle_l):
            return item
    return None


def _chapters_from_book_dir(book_dir: Path) -> list[dict[str, Any]]:
    """Chapter markers from libraforge.json Chapter Forge sidecar (if present)."""
    lf_path = Path(book_dir) / "libraforge.json"
    if not lf_path.is_file():
        return []
    try:
        lf = json.loads(lf_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(lf, dict):
        return []
    cf = lf.get("chapter_forge") if isinstance(lf.get("chapter_forge"), dict) else {}
    raw = cf.get("chapters") if isinstance(cf.get("chapters"), list) else []
    out: list[dict[str, Any]] = []
    for i, ch in enumerate(raw):
        if not isinstance(ch, dict):
            continue
        title = str(ch.get("title") or "").strip()
        try:
            start = float(ch.get("start"))
        except (TypeError, ValueError):
            continue
        try:
            end = float(ch.get("end")) if ch.get("end") is not None else None
        except (TypeError, ValueError):
            end = None
        entry: dict[str, Any] = {
            "id": int(ch.get("id") if ch.get("id") is not None else i),
            "start": start,
            "title": title or f"Chapter {i + 1}",
        }
        if end is not None:
            entry["end"] = end
        out.append(entry)
    return out


def _metadata_payload_from_book_dir(book_dir: Path) -> tuple[dict[str, Any], str | None]:
    """Build ABS metadata PATCH payload + optional cover URL from on-disk sidecars.

    Only non-empty fields are included so ABS field-merge PATCH cannot wipe good
    values with blanks. LibraForge stores the blurb as ``summary``; map that to
    ``description``. Skip series when the label equals the title (Audible often
    repeats the book name as a pseudo-series).
    """
    root = Path(book_dir)
    cover_url: str | None = None
    meta: dict[str, Any] = {}

    meta_path = root / "metadata.json"
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = dict(loaded)
        except (OSError, json.JSONDecodeError):
            pass

    lf_path = root / "libraforge.json"
    if lf_path.is_file():
        try:
            lf = json.loads(lf_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lf = None
        if isinstance(lf, dict):
            marker = lf.get("marker") if isinstance(lf.get("marker"), dict) else {}
            audible = marker.get("audible") if isinstance(marker.get("audible"), dict) else {}
            # Prefer LibraForge audible apply fields over older metadata.json.
            title = audible.get("title") or audible.get("chosen_title")
            if title:
                meta["title"] = title
            for src_key, dst_key in (
                ("subtitle", "subtitle"),
                ("publisher", "publisher"),
                ("asin", "asin"),
                ("year", "publishedYear"),
                ("description", "description"),
            ):
                val = audible.get(src_key)
                if val:
                    meta[dst_key] = val
            # LibraForge Manual Review / Metadata Forge store the blurb as summary.
            if not meta.get("description") and audible.get("summary"):
                meta["description"] = audible["summary"]
            if audible.get("author"):
                meta["authors"] = [audible["author"]]
            if audible.get("narrator"):
                meta["narrators"] = [audible["narrator"]]
            series_val = str(audible.get("series") or "").strip()
            title_for_series = str(
                audible.get("title") or audible.get("chosen_title") or meta.get("title") or ""
            ).strip()
            # Audible often sets series == title for standalone books — do not push that.
            if series_val and series_val.casefold() != title_for_series.casefold():
                seq = str(audible.get("sequence") or "").strip()
                label = f"{series_val} #{seq}" if seq else series_val
                meta["series"] = [label]
            genre = audible.get("genre")
            if genre:
                meta["genres"] = [g.strip() for g in str(genre).split(",") if g.strip()]
            cover_url = (
                str(audible.get("cover_url") or "").strip()
                or str(marker.get("cover_url") or "").strip()
                or None
            )

    payload: dict[str, Any] = {}
    title = str(meta.get("title") or "").strip()
    if title:
        payload["title"] = title
    subtitle = str(meta.get("subtitle") or "").strip()
    if subtitle:
        payload["subtitle"] = subtitle
    asin = str(meta.get("asin") or "").strip()
    if asin:
        payload["asin"] = asin
    publisher = str(meta.get("publisher") or "").strip()
    if publisher:
        payload["publisher"] = publisher
    year = str(meta.get("publishedYear") or meta.get("year") or "").strip()
    if year:
        payload["publishedYear"] = year
    description = str(meta.get("description") or meta.get("summary") or "").strip()
    if description:
        payload["description"] = description

    authors_raw = meta.get("authors") or meta.get("author")
    authors: list[str] = []
    if isinstance(authors_raw, str) and authors_raw.strip():
        authors = [authors_raw.strip()]
    elif isinstance(authors_raw, list):
        for a in authors_raw:
            if isinstance(a, str) and a.strip():
                authors.append(a.strip())
            elif isinstance(a, dict) and str(a.get("name") or "").strip():
                authors.append(str(a["name"]).strip())
    if authors:
        payload["authors"] = [{"name": a} for a in authors]

    narrators_raw = meta.get("narrators") or meta.get("narrator")
    narrators: list[str] = []
    if isinstance(narrators_raw, str) and narrators_raw.strip():
        narrators = [narrators_raw.strip()]
    elif isinstance(narrators_raw, list):
        for n in narrators_raw:
            if isinstance(n, str) and n.strip():
                narrators.append(n.strip())
            elif isinstance(n, dict) and str(n.get("name") or "").strip():
                narrators.append(str(n["name"]).strip())
    if narrators:
        payload["narrators"] = narrators

    series_raw = meta.get("series")
    series_out: list[dict[str, str]] = []
    if isinstance(series_raw, str) and series_raw.strip():
        name, seq = parse_abs_series_label(series_raw.strip())
        if name and name.casefold() != title.casefold():
            entry: dict[str, str] = {"name": name}
            if seq:
                entry["sequence"] = seq
            series_out.append(entry)
    elif isinstance(series_raw, list):
        for s in series_raw:
            if isinstance(s, str) and s.strip():
                name, seq = parse_abs_series_label(s.strip())
                if name and name.casefold() != title.casefold():
                    entry = {"name": name}
                    if seq:
                        entry["sequence"] = seq
                    series_out.append(entry)
            elif isinstance(s, dict) and str(s.get("name") or "").strip():
                name = str(s["name"]).strip()
                if name.casefold() == title.casefold():
                    continue
                entry = {"name": name}
                seq = str(s.get("sequence") or "").strip()
                if seq:
                    entry["sequence"] = seq
                series_out.append(entry)
    if series_out:
        payload["series"] = series_out

    genres = meta.get("genres")
    if isinstance(genres, list):
        cleaned = [
            str(g).strip()
            for g in genres
            if str(g).strip() and str(g).strip().casefold() != "audiobook"
        ]
        if cleaned:
            payload["genres"] = cleaned
    elif isinstance(genres, str) and genres.strip() and genres.strip().casefold() != "audiobook":
        payload["genres"] = [g.strip() for g in genres.split(",") if g.strip()]

    return payload, cover_url


async def update_item_chapters(item_id: str, chapters: list[dict[str, Any]]) -> bool:
    """POST chapter markers into ABS (pins absMetadata chapters after scan)."""
    if not item_id or not chapters:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.abs_url}/api/items/{item_id}/chapters",
                headers=_headers(),
                json={"chapters": chapters},
                timeout=60,
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning("ABS update chapters for %s failed: %s", item_id, e)
        return False


async def set_item_cover_from_url(item_id: str, cover_url: str) -> bool:
    """Ask ABS to download a cover from a URL (POST /api/items/:id/cover)."""
    url = (cover_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.abs_url}/api/items/{item_id}/cover",
                headers=_headers(),
                json={"url": url},
                timeout=60,
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning("ABS set cover for %s failed: %s", item_id, e)
        return False


async def sync_book_dir_metadata_to_abs(book_dir: str | Path) -> dict[str, Any]:
    """Push LibraForge / folder sidecar metadata into ABS for one library book dir.

    Scan-only does not rematch online providers; this pins ABS DB fields to our
    applied tags so later scans keep ``absMetadata`` precedence instead of
    folder-name drift. Never calls Quick Match. Only non-empty sidecar fields are
    patched (ABS merges). Chapter Forge markers are pushed when present.
    """
    root = Path(book_dir)
    out: dict[str, Any] = {
        "path": str(root),
        "updated": False,
        "cover_updated": False,
        "chapters_updated": False,
        "item_id": None,
        "error": None,
    }
    if not root.is_dir():
        out["error"] = "book dir missing"
        return out

    audiobook_root = Path(settings.audiobook_dir).resolve()
    try:
        rel = root.resolve().relative_to(audiobook_root)
    except (ValueError, OSError):
        # Paths may already be container-style under AUDIOBOOK_DIR (default /audiobooks/...).
        root_s = str(root).replace("\\", "/")
        mount = str(settings.audiobook_dir).replace("\\", "/").rstrip("/")
        marker = f"{mount}/" if mount else "/audiobooks/"
        if marker in root_s:
            rel_s = root_s.split(marker, 1)[1]
        elif "/audiobooks/" in root_s:
            rel_s = root_s.split("/audiobooks/", 1)[1]
        else:
            rel_s = root_s.lstrip("/")
        rel = Path(rel_s)
    rel_path = rel.as_posix().strip("/")

    item = await find_item_by_rel_path(rel_path)
    if not item:
        out["error"] = f"ABS item not found for relPath={rel_path}"
        return out
    item_id = str(item.get("id") or "").strip()
    out["item_id"] = item_id or None
    if not item_id:
        out["error"] = "ABS item missing id"
        return out

    payload, cover_url = _metadata_payload_from_book_dir(root)
    if payload:
        ok = await update_item_metadata(item_id, metadata=payload)
        out["updated"] = ok
        if not ok:
            out["error"] = "metadata PATCH failed"
    if cover_url:
        out["cover_updated"] = await set_item_cover_from_url(item_id, cover_url)
    chapters = _chapters_from_book_dir(root)
    if chapters:
        out["chapters_updated"] = await update_item_chapters(item_id, chapters)
    if out["updated"] or out["cover_updated"] or out["chapters_updated"]:
        invalidate_cache()
    return out


async def sync_organizer_moves_to_abs(organizer_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """After Folder Forge, push sidecar metadata for each successful move target."""
    from app.services import libraforge as lf

    results: list[dict[str, Any]] = []
    for target in lf.organizer_move_targets(organizer_report or {}):
        try:
            results.append(await sync_book_dir_metadata_to_abs(target))
        except Exception as e:
            logger.warning("ABS metadata sync failed for %s: %s", target, e)
            results.append({"path": target, "updated": False, "error": str(e)})
    return results


async def fix_metadata_mismatches(library_id: str | None = None) -> dict[str, Any]:
    """Scan ABS (wait for completion) and remove missing-file orphans.

    Intentionally does **not** rewrite titles to folder names — that overwrote
    LibraForge / Audible titles (e.g. ``Illidan: World of Warcraft`` → ``Illidan``).
    Scan alone does not Quick Match online providers. Re-applies ABS hardening
    settings (skip ASIN match, absMetadata precedence) before scanning.

    Returns keys: ``fixed``, ``count``, ``scan_ran``, ``scan_complete``, ``timed_out``,
    ``waited_seconds``, ``items_total``, ``orphan_cleanup_ok``, ``items_examined``,
    ``fetch_error`` (set when the item list could not be loaded), ``hardening``.
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
        "hardening": None,
    }
    if not lid or not settings.abs_api_key:
        empty["fetch_error"] = "Audiobookshelf library is not configured"
        return empty

    hardening = await ensure_metadata_hardening(lid)

    scan_ran = False
    scan_complete = False
    timed_out = False
    waited_seconds = 0.0
    items_total: int | None = None
    orphan_cleanup_ok = False
    try:
        # Prefer deferred kick — blocking wait + full item pagination after a
        # scan has OOM'd / rebooted the Pi when admin hammered "Scan ABS".
        kick = await kick_library_scan(wait=False)
        scan_ran = bool(kick.get("scan_ran"))
        scan_complete = bool(kick.get("scan_complete"))
        timed_out = bool(kick.get("timed_out"))
        waited_seconds = float(kick.get("waited_seconds") or 0)
        items_total = kick.get("items_total")
        # Orphan cleanup runs inside the background task after scan finishes.
        orphan_cleanup_ok = bool(kick.get("deferred") or kick.get("scan_complete"))
        if kick.get("error"):
            logger.warning("fix_metadata_mismatches: %s", kick.get("error"))
    except Exception as e:
        logger.warning("fix_metadata_mismatches: library scan / orphan cleanup failed: %s", e)

    # Cheap total — never paginate the whole library right after kicking a scan.
    items_examined = 0
    try:
        total = await get_library_item_total(lid)
        if total is not None:
            items_total = total
            items_examined = total
    except Exception as e:
        logger.warning("Failed to fetch ABS item total after scan kick: %s", e)

    if scan_ran or orphan_cleanup_ok:
        invalidate_cache()

    return {
        "fixed": [],
        "count": 0,
        "scan_ran": scan_ran,
        "scan_complete": scan_complete,
        "timed_out": timed_out,
        "waited_seconds": waited_seconds,
        "items_total": items_total,
        "orphan_cleanup_ok": orphan_cleanup_ok,
        "items_examined": items_examined,
        "fetch_error": None,
        "hardening": hardening,
        "deferred": True,
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
