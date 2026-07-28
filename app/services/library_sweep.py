"""Admin Library Sweep — backfill ABS audiobooks through forge (no download)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.database import async_session
from app.models import DownloadRequest, LibrarySweepJob
from app.services import audiobookshelf, library_ingest
from app.services.library_media_delete import resolve_abs_book_dir

logger = logging.getLogger(__name__)
settings = get_settings()

_worker_task: asyncio.Task | None = None
_worker_lock = asyncio.Lock()

# Book the sweep worker is currently on (metadata / sync forge — not M4B waiters).
_current: dict[str, Any] | None = None
_skip_request_ids: set[int] = set()

# Completed sweep books since the last batched ABS scan (includes M4B handoffs).
_completed_since_abs_scan = 0
_abs_scan_lock = asyncio.Lock()


def _abs_scan_every() -> int:
    """How many completed books between full ABS scans (min 1)."""
    raw = getattr(settings, "library_sweep_abs_scan_every", 25)
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 25
    return max(1, n)


async def run_batched_abs_scan(*, reason: str = "sweep") -> dict[str, Any]:
    """One full ABS library scan + orphan cleanup (used by Sweep cadence)."""
    global _completed_since_abs_scan
    async with _abs_scan_lock:
        logger.info("Library Sweep ABS batch scan (%s)", reason)
        try:
            status = await audiobookshelf.scan_library_and_wait(timeout_seconds=240)
            await audiobookshelf.remove_items_with_issues()
            audiobookshelf.invalidate_cache()
            _completed_since_abs_scan = 0
            return {"ok": True, "reason": reason, "scan": status}
        except Exception as e:
            logger.warning("Library Sweep ABS batch scan failed (%s): %s", reason, e)
            return {"ok": False, "reason": reason, "error": str(e)[:300]}


async def on_sweep_book_finalized() -> None:
    """Called from forge finalize when a sweep book completes without a per-book ABS scan."""
    global _completed_since_abs_scan
    _completed_since_abs_scan += 1
    every = _abs_scan_every()
    if _completed_since_abs_scan >= every:
        await run_batched_abs_scan(
            reason=f"every {every} completed (at {_completed_since_abs_scan})"
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _set_current(
    *,
    request_id: int | None = None,
    title: str | None = None,
    author: str | None = None,
    cover_url: str | None = None,
    status: str | None = None,
    abs_item_id: str | None = None,
) -> None:
    global _current
    if request_id is None and title is None:
        _current = None
        return
    _current = {
        "request_id": request_id,
        "title": title,
        "author": author,
        "cover_url": cover_url,
        "status": status or "processing",
        "abs_item_id": abs_item_id,
    }


async def _refresh_current_from_db(request_id: int) -> None:
    """Pull latest title/cover/status after metadata forge updates the row."""
    global _current
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
    if not req:
        return
    _current = {
        "request_id": req.id,
        "title": req.title,
        "author": req.author,
        "cover_url": req.cover_url,
        "status": req.status,
        "abs_item_id": req.abs_item_id,
    }


async def get_or_create_job() -> LibrarySweepJob:
    async with async_session() as db:
        result = await db.execute(
            select(LibrarySweepJob).order_by(LibrarySweepJob.id.desc()).limit(1)
        )
        job = result.scalar_one_or_none()
        if job:
            return job
        job = LibrarySweepJob(status="idle", updated_at=_utcnow())
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job


async def _load_job(job_id: int) -> LibrarySweepJob | None:
    async with async_session() as db:
        result = await db.execute(
            select(LibrarySweepJob).where(LibrarySweepJob.id == job_id)
        )
        return result.scalar_one_or_none()


async def _update_job(job_id: int, **fields: Any) -> LibrarySweepJob | None:
    async with async_session() as db:
        result = await db.execute(
            select(LibrarySweepJob).where(LibrarySweepJob.id == job_id)
        )
        job = result.scalar_one_or_none()
        if not job:
            return None
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.updated_at = _utcnow()
        await db.commit()
        await db.refresh(job)
        return job


def _request_to_unprocessed(r: DownloadRequest) -> dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "author": r.author,
        "status": r.status,
        "status_detail": r.status_detail,
        "quarantine_reason": r.quarantine_reason,
        "abs_item_id": r.abs_item_id,
        "ingest_fingerprint": r.ingest_fingerprint,
        "staging_path": r.staging_path,
        "cover_url": r.cover_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _unprocessed_counts() -> dict[str, int]:
    statuses = tuple(library_ingest._UNPROCESSED_STATUSES)
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest.status, func.count())
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.status.in_(statuses),
            )
            .group_by(DownloadRequest.status)
        )
        by_status = {row[0]: int(row[1]) for row in result.all()}
    return {
        "cancelled": by_status.get("cancelled", 0),
        "failed": by_status.get("failed", 0),
        "skipped": by_status.get("skipped", 0),
        "admin_rejected": by_status.get("admin_rejected", 0),
        "total": sum(by_status.values()),
    }


async def _resolve_current_book() -> dict[str, Any] | None:
    """Prefer in-memory worker cursor; fall back to newest active sweep forge row."""
    if _current and _current.get("request_id"):
        rid = int(_current["request_id"])
        await _refresh_current_from_db(rid)
        if _current:
            return dict(_current)
    if _current and (_current.get("title") or _current.get("abs_item_id")):
        return dict(_current)

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.status.in_(
                    (
                        "metadata_forge",
                        "m4b_convert",
                        "chapter_forge",
                        "folder_forge",
                        "finalizing",
                    )
                ),
            )
            .order_by(DownloadRequest.id.desc())
            .limit(1)
        )
        req = result.scalar_one_or_none()
    if not req:
        return None
    return {
        "request_id": req.id,
        "title": req.title,
        "author": req.author,
        "cover_url": req.cover_url,
        "status": req.status,
        "abs_item_id": req.abs_item_id,
    }


def job_to_dict(job: LibrarySweepJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "total": int(job.total or 0),
        "scanned": int(job.scanned or 0),
        "auto_applied": int(job.auto_applied or 0),
        "needs_review": int(job.needs_review or 0),
        "failed": int(job.failed or 0),
        "m4b_queued": int(job.m4b_queued or 0),
        "review_cursor_request_id": job.review_cursor_request_id,
        "error": job.error,
        "started_by_user_id": job.started_by_user_id,
    }


async def get_status() -> dict[str, Any]:
    job = await get_or_create_job()
    data = job_to_dict(job)
    data["current"] = await _resolve_current_book()
    data["unprocessed"] = await _unprocessed_counts()
    return data


async def set_review_cursor(request_id: int | None) -> dict[str, Any]:
    job = await get_or_create_job()
    updated = await _update_job(job.id, review_cursor_request_id=request_id)
    return await get_status() if updated else await get_status()


async def list_needs_review(*, limit: int = 200) -> list[dict[str, Any]]:
    """Quarantined DownloadRequests created by Library Sweep."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.status == "quarantined",
            )
            .order_by(DownloadRequest.id.asc())
            .limit(max(1, min(limit, 1000)))
        )
        rows = list(result.scalars().all())
    return [_request_to_unprocessed(r) for r in rows]


async def list_unprocessed(*, limit: int = 500) -> list[dict[str, Any]]:
    """Cancelled / failed / skipped / admin_rejected sweep books for manual reprocess."""
    statuses = tuple(library_ingest._UNPROCESSED_STATUSES)
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.status.in_(statuses),
            )
            .order_by(DownloadRequest.id.desc())
            .limit(max(1, min(limit, 2000)))
        )
        rows = list(result.scalars().all())
    return [_request_to_unprocessed(r) for r in rows]


async def start_sweep(*, user_id: int) -> dict[str, Any]:
    """Start or resume a sweep. Returns job status dict."""
    global _worker_task, _completed_since_abs_scan
    job = await get_or_create_job()

    if job.status == "running":
        # Worker may have died after cancel/deploy — restart if needed.
        async with _worker_lock:
            if _worker_task is None or _worker_task.done():
                _worker_task = asyncio.create_task(
                    _run_sweep_worker(job.id), name="library-sweep"
                )
                return {**(await get_status()), "message": "Sweep worker restarted"}
        return {**(await get_status()), "message": "Sweep already running"}

    if job.status == "paused":
        updated = await _update_job(
            job.id,
            status="running",
            error=None,
            started_by_user_id=user_id,
        )
        async with _worker_lock:
            if _worker_task is None or _worker_task.done():
                _worker_task = asyncio.create_task(
                    _run_sweep_worker(job.id), name="library-sweep"
                )
        return {**(await get_status()), "message": "Sweep resumed"}

    # Fresh start (idle / completed / cancelled)
    _skip_request_ids.clear()
    _set_current()
    _completed_since_abs_scan = 0
    updated = await _update_job(
        job.id,
        status="running",
        started_at=_utcnow(),
        started_by_user_id=user_id,
        error=None,
        total=0,
        scanned=0,
        auto_applied=0,
        needs_review=0,
        failed=0,
        m4b_queued=0,
    )
    async with _worker_lock:
        if _worker_task is not None and not _worker_task.done():
            _worker_task.cancel()
        _worker_task = asyncio.create_task(
            _run_sweep_worker(job.id), name="library-sweep"
        )
    return {**(await get_status()), "message": "Sweep started"}


async def pause_sweep() -> dict[str, Any]:
    job = await get_or_create_job()
    if job.status != "running":
        return {**(await get_status()), "message": f"Cannot pause from status '{job.status}'"}
    await _update_job(job.id, status="paused")
    return {**(await get_status()), "message": "Sweep pausing after current book"}


async def cancel_sweep() -> dict[str, Any]:
    job = await get_or_create_job()
    if job.status not in ("running", "paused"):
        return {**(await get_status()), "message": f"Cannot cancel from status '{job.status}'"}
    was_paused = job.status == "paused"
    await _update_job(job.id, status="cancelled")
    _set_current()
    # Worker may already have exited (paused) — still flush a batch scan.
    if was_paused:
        await run_batched_abs_scan(reason="sweep cancelled")
    return {**(await get_status()), "message": "Sweep cancelled"}


async def skip_current(*, request_id: int | None = None) -> dict[str, Any]:
    """Mark the current (or given) sweep book as skipped and abort its forge."""
    rid = request_id
    if rid is None and _current and _current.get("request_id"):
        rid = int(_current["request_id"])
    if rid is None:
        # Fall back to newest active sweep forge row
        current = await _resolve_current_book()
        if current and current.get("request_id"):
            rid = int(current["request_id"])
    if rid is None:
        return {**(await get_status()), "message": "Nothing to skip", "skipped_id": None}

    _skip_request_ids.add(rid)
    forge_run_id: str | None = None
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == rid)
        )
        req = result.scalar_one_or_none()
        if not req:
            return {**(await get_status()), "message": "Request not found", "skipped_id": rid}
        if req.source != library_ingest.SOURCE_SWEEP:
            return {
                **(await get_status()),
                "message": "Not a sweep request",
                "skipped_id": rid,
            }
        forge_run_id = (getattr(req, "libraforge_run_id", None) or "").strip() or None
        req.status = "skipped"
        req.status_detail = "Skipped during Library Sweep"
        req.progress_percent = None
        req.progress_bytes = None
        req.progress_total_bytes = None
        req.progress_speed_bps = None
        await db.commit()

    if forge_run_id:
        try:
            from app.services import libraforge

            await libraforge.cancel_run(forge_run_id)
        except Exception:
            logger.debug("LibraForge cancel_run for skip %s failed", rid, exc_info=True)

    if _current and _current.get("request_id") == rid:
        _set_current()

    return {
        **(await get_status()),
        "message": f"Skipped request #{rid}",
        "skipped_id": rid,
    }


async def reprocess_unprocessed(request_id: int, *, user_id: int) -> dict[str, Any]:
    """Kick forge again for a cancelled/failed/skipped sweep book."""
    try:
        result = await library_ingest.reprocess_local_ingest(
            request_id,
            handoff_m4b=True,
        )
    except FileNotFoundError as e:
        raise
    except ValueError as e:
        raise
    return {
        "ok": bool(result.get("ok", True)),
        "result": result,
        "status": await get_status(),
    }


async def resume_running_sweep_on_startup() -> None:
    """If a sweep was mid-run when the process died, restart the worker."""
    global _worker_task
    job = await get_or_create_job()
    if job.status != "running":
        return
    async with _worker_lock:
        if _worker_task is not None and not _worker_task.done():
            return
        logger.info("Library Sweep: restarting worker for job %s after startup", job.id)
        _worker_task = asyncio.create_task(
            _run_sweep_worker(job.id), name="library-sweep"
        )


async def _enumerate_abs_books() -> list[dict[str, Any]]:
    """Raw ABS library items that look like books (have title + path)."""
    lid = settings.abs_library_id
    if not lid or not settings.abs_api_key:
        return []
    try:
        raw = await audiobookshelf._fetch_library_items_all_pages(lid)
    except Exception:
        logger.warning("Library Sweep: failed to enumerate ABS items", exc_info=True)
        return []
    books: list[dict[str, Any]] = []
    for item in raw:
        media = item.get("media") or {}
        meta = media.get("metadata") or {}
        title = (meta.get("title") or meta.get("titleIgnorePrefix") or "").strip()
        if not title:
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        books.append(item)
    return books


async def _run_sweep_worker(job_id: int) -> None:
    try:
        books = await _enumerate_abs_books()
        await _update_job(job_id, total=len(books))
        logger.info("Library Sweep job %s: %s ABS items", job_id, len(books))

        for index, item in enumerate(books):
            job = await _load_job(job_id)
            if not job:
                return
            if job.status == "paused":
                logger.info("Library Sweep job %s paused at index %s", job_id, index)
                _set_current()
                await run_batched_abs_scan(reason="sweep paused")
                return
            if job.status == "cancelled":
                logger.info("Library Sweep job %s cancelled at index %s", job_id, index)
                _set_current()
                await run_batched_abs_scan(reason="sweep cancelled")
                return
            if job.status != "running":
                _set_current()
                await run_batched_abs_scan(reason=f"sweep stopped ({job.status})")
                return

            try:
                await _process_one_item(job_id, item, scan_index=index)
            except asyncio.CancelledError:
                # Worker task cancelled (fresh start / shutdown) — re-raise.
                # Per-request cancel raises LibraForgeError, not CancelledError.
                raise
            except Exception as e:
                logger.exception("Library Sweep item failed: %s", e)
                job = await _load_job(job_id)
                if job:
                    await _update_job(
                        job_id,
                        scanned=index + 1,
                        failed=int(job.failed or 0) + 1,
                        error=str(e)[:500],
                    )

        job = await _load_job(job_id)
        if job and job.status == "running":
            await _update_job(job_id, status="completed", error=None)
            logger.info("Library Sweep job %s completed", job_id)
            await run_batched_abs_scan(reason="sweep completed")
        _set_current()
    except asyncio.CancelledError:
        logger.info("Library Sweep worker cancelled")
        _set_current()
        await run_batched_abs_scan(reason="sweep stopped")
        raise
    except Exception as e:
        logger.exception("Library Sweep worker crashed: %s", e)
        job = await _load_job(job_id)
        if job and job.status == "running":
            await _update_job(job_id, status="completed", error=str(e)[:500])
        _set_current()
        await run_batched_abs_scan(reason="sweep error stop")


async def _bump_failed(job_id: int, scan_index: int, error: str) -> None:
    job = await _load_job(job_id)
    if not job:
        return
    await _update_job(
        job_id,
        scanned=scan_index + 1,
        failed=int(job.failed or 0) + 1,
        error=error[:500],
    )


async def _process_one_item(
    job_id: int, item: dict[str, Any], *, scan_index: int
) -> None:
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    item_id = str(item.get("id") or "").strip()
    title = (meta.get("title") or meta.get("titleIgnorePrefix") or "Untitled").strip()
    author = (meta.get("authorName") or "").strip() or None
    fingerprint = library_ingest.sweep_fingerprint(item_id)
    cover_url = f"/api/stream/abs/proxy/cover/{item_id}" if item_id else None

    _set_current(
        request_id=None,
        title=title,
        author=author,
        cover_url=cover_url,
        status="scanning",
        abs_item_id=item_id,
    )

    if await library_ingest.fingerprint_already_swept(fingerprint):
        await _update_job(job_id, scanned=scan_index + 1)
        return

    if await library_ingest.fingerprint_in_flight(fingerprint):
        # M4B handoff / forge still running — count as scanned, do not re-kick.
        await _update_job(job_id, scanned=scan_index + 1)
        return

    job = await _load_job(job_id)
    if not job or not job.started_by_user_id:
        raise RuntimeError("Sweep job missing started_by_user_id")
    user_id = int(job.started_by_user_id)

    existing = await library_ingest.latest_sweep_request(fingerprint)
    if existing and existing.status == "quarantined":
        _set_current(
            request_id=existing.id,
            title=existing.title or title,
            author=existing.author or author,
            cover_url=existing.cover_url or cover_url,
            status=existing.status,
            abs_item_id=item_id,
        )
        result = await library_ingest.retry_quarantined_ingest(
            existing.id,
            user_id=user_id,
            title=title,
            author=author,
            handoff_m4b=True,
        )
        await _refresh_current_from_db(existing.id)
        await _record_sweep_result(job_id, result, scan_index=scan_index)
        return

    if existing and existing.status == "completed":
        await _update_job(job_id, scanned=scan_index + 1)
        return

    # Cancelled / failed / skipped / rejected — leave for Unprocessed tab.
    if existing and existing.status in library_ingest._UNPROCESSED_STATUSES:
        await _update_job(job_id, scanned=scan_index + 1)
        logger.debug(
            "Sweep skip unprocessed fingerprint %s (status=%s)",
            fingerprint,
            existing.status,
        )
        return

    root = Path(settings.audiobook_dir)
    try:
        library_dir = resolve_abs_book_dir(root, item)
    except ValueError as e:
        logger.warning("Sweep skip %s (%s): %s", item_id, title, e)
        await _bump_failed(job_id, scan_index, f"{title}: {e}")
        return

    if not library_dir.is_dir():
        await _bump_failed(job_id, scan_index, f"{title}: library folder missing")
        return

    async def _on_created(rid: int) -> None:
        _set_current(
            request_id=rid,
            title=title,
            author=author,
            cover_url=cover_url,
            status="metadata_forge",
            abs_item_id=item_id,
        )

    result = await library_ingest.ingest_from_library_folder(
        user_id=user_id,
        title=title,
        author=author,
        library_dir=library_dir,
        source=library_ingest.SOURCE_SWEEP,
        magnet_link=library_ingest.sweep_magnet(item_id),
        abs_item_id=item_id,
        ingest_fingerprint=fingerprint,
        cover_url=cover_url,
        kick_forge=True,
        handoff_m4b=True,
        on_request_created=_on_created,
    )

    rid = result.get("id")
    if rid:
        if rid in _skip_request_ids or await _request_is_skipped(int(rid)):
            await _record_sweep_result(
                job_id,
                {**result, "status": "skipped"},
                scan_index=scan_index,
            )
            _skip_request_ids.discard(int(rid))
            return
        await _refresh_current_from_db(int(rid))

    await _record_sweep_result(job_id, result, scan_index=scan_index)


async def _request_is_skipped(request_id: int) -> bool:
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest.status).where(DownloadRequest.id == request_id)
        )
        return result.scalar_one_or_none() == "skipped"


async def _record_sweep_result(
    job_id: int, result: dict[str, Any], *, scan_index: int
) -> None:
    job = await _load_job(job_id)
    if not job:
        return

    # Absolute position — never accumulate on resume re-walks.
    scanned = scan_index + 1
    auto_applied = int(job.auto_applied or 0)
    needs_review = int(job.needs_review or 0)
    failed = int(job.failed or 0)
    m4b_queued = int(job.m4b_queued or 0)

    status = result.get("status") or ""
    retried = bool(result.get("retried"))
    handed_off = bool(result.get("m4b_handed_off")) or (
        status == "m4b_convert" and bool(result.get("needs_m4b"))
    )

    if handed_off or (result.get("needs_m4b") and status == "m4b_convert"):
        m4b_queued += 1

    if status == "completed":
        auto_applied += 1
        if retried:
            needs_review = max(0, needs_review - 1)
    elif status == "quarantined":
        if not retried:
            needs_review += 1
        cursor = job.review_cursor_request_id
        if cursor is None and result.get("id"):
            await _update_job(job_id, review_cursor_request_id=int(result["id"]))
            job = await _load_job(job_id) or job
    elif status == "skipped":
        pass  # tracked via DownloadRequest; do not inflate failed
    elif status in ("failed", "admin_rejected", "cancelled"):
        failed += 1
    elif status == "m4b_convert" and handed_off:
        # Background pipeline will finish — not a failure, not yet auto-applied.
        pass
    elif status in ("chapter_forge", "folder_forge", "finalizing", "metadata_forge"):
        # Sync path finished mid-pipeline somehow (or no-M4B still running).
        if status != "metadata_forge":
            auto_applied += 1
        if retried:
            needs_review = max(0, needs_review - 1)
    else:
        failed += 1

    await _update_job(
        job_id,
        scanned=scanned,
        auto_applied=auto_applied,
        needs_review=needs_review,
        failed=failed,
        m4b_queued=m4b_queued,
        error=None,
    )
