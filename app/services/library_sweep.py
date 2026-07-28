"""Admin Library Sweep — backfill ABS audiobooks through forge (no download)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import DownloadRequest, LibrarySweepJob
from app.services import audiobookshelf, library_ingest
from app.services.library_media_delete import resolve_abs_book_dir

logger = logging.getLogger(__name__)
settings = get_settings()

_worker_task: asyncio.Task | None = None
_worker_lock = asyncio.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    return job_to_dict(job)


async def set_review_cursor(request_id: int | None) -> dict[str, Any]:
    job = await get_or_create_job()
    updated = await _update_job(job.id, review_cursor_request_id=request_id)
    return job_to_dict(updated or job)


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
    return [
        {
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
        for r in rows
    ]


async def start_sweep(*, user_id: int) -> dict[str, Any]:
    """Start or resume a sweep. Returns job status dict."""
    global _worker_task
    job = await get_or_create_job()

    if job.status == "running":
        return {**job_to_dict(job), "message": "Sweep already running"}

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
        return {**(job_to_dict(updated or job)), "message": "Sweep resumed"}

    # Fresh start (idle / completed / cancelled)
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
    return {**(job_to_dict(updated or job)), "message": "Sweep started"}


async def pause_sweep() -> dict[str, Any]:
    job = await get_or_create_job()
    if job.status != "running":
        return {**job_to_dict(job), "message": f"Cannot pause from status '{job.status}'"}
    updated = await _update_job(job.id, status="paused")
    return {**(job_to_dict(updated or job)), "message": "Sweep pausing after current book"}


async def cancel_sweep() -> dict[str, Any]:
    job = await get_or_create_job()
    if job.status not in ("running", "paused"):
        return {**job_to_dict(job), "message": f"Cannot cancel from status '{job.status}'"}
    updated = await _update_job(job.id, status="cancelled")
    return {**(job_to_dict(updated or job)), "message": "Sweep cancelled"}


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

        for item in books:
            job = await _load_job(job_id)
            if not job:
                return
            if job.status == "paused":
                logger.info("Library Sweep job %s paused", job_id)
                return
            if job.status == "cancelled":
                logger.info("Library Sweep job %s cancelled", job_id)
                return
            if job.status != "running":
                return

            try:
                await _process_one_item(job_id, item)
            except Exception as e:
                logger.exception("Library Sweep item failed: %s", e)
                job = await _load_job(job_id)
                if job:
                    await _update_job(
                        job_id,
                        scanned=int(job.scanned or 0) + 1,
                        failed=int(job.failed or 0) + 1,
                        error=str(e)[:500],
                    )

        job = await _load_job(job_id)
        if job and job.status == "running":
            await _update_job(job_id, status="completed", error=None)
            logger.info("Library Sweep job %s completed", job_id)
    except asyncio.CancelledError:
        logger.info("Library Sweep worker cancelled")
        raise
    except Exception as e:
        logger.exception("Library Sweep worker crashed: %s", e)
        job = await _load_job(job_id)
        if job and job.status == "running":
            await _update_job(job_id, status="completed", error=str(e)[:500])


async def _bump_failed(job_id: int, error: str) -> None:
    job = await _load_job(job_id)
    if not job:
        return
    await _update_job(
        job_id,
        scanned=int(job.scanned or 0) + 1,
        failed=int(job.failed or 0) + 1,
        error=error[:500],
    )


async def _process_one_item(job_id: int, item: dict[str, Any]) -> None:
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    item_id = str(item.get("id") or "").strip()
    title = (meta.get("title") or meta.get("titleIgnorePrefix") or "Untitled").strip()
    author = (meta.get("authorName") or "").strip() or None
    fingerprint = library_ingest.sweep_fingerprint(item_id)

    if await library_ingest.fingerprint_already_swept(fingerprint):
        job = await _load_job(job_id)
        if job:
            await _update_job(job_id, scanned=int(job.scanned or 0) + 1)
        logger.debug("Sweep skip already completed fingerprint %s", fingerprint)
        return

    if await library_ingest.fingerprint_in_flight(fingerprint):
        job = await _load_job(job_id)
        if job:
            await _update_job(job_id, scanned=int(job.scanned or 0) + 1)
        return

    job = await _load_job(job_id)
    if not job or not job.started_by_user_id:
        raise RuntimeError("Sweep job missing started_by_user_id")
    user_id = int(job.started_by_user_id)

    # Resume / re-run: reuse quarantined sweep rows instead of creating duplicates.
    existing = await library_ingest.latest_sweep_request(fingerprint)
    if existing and existing.status == "quarantined":
        result = await library_ingest.retry_quarantined_ingest(
            existing.id,
            user_id=user_id,
            title=title,
            author=author,
        )
        await _record_sweep_result(job_id, result)
        return

    if existing and existing.status == "completed":
        job = await _load_job(job_id)
        if job:
            await _update_job(job_id, scanned=int(job.scanned or 0) + 1)
        return

    root = Path(settings.audiobook_dir)
    try:
        library_dir = resolve_abs_book_dir(root, item)
    except ValueError as e:
        logger.warning("Sweep skip %s (%s): %s", item_id, title, e)
        await _bump_failed(job_id, f"{title}: {e}")
        return

    if not library_dir.is_dir():
        await _bump_failed(job_id, f"{title}: library folder missing")
        return

    cover_url = f"/api/stream/abs/proxy/cover/{item_id}" if item_id else None
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
    )

    await _record_sweep_result(job_id, result)


async def _record_sweep_result(job_id: int, result: dict[str, Any]) -> None:
    job = await _load_job(job_id)
    if not job:
        return

    scanned = int(job.scanned or 0) + 1
    auto_applied = int(job.auto_applied or 0)
    needs_review = int(job.needs_review or 0)
    failed = int(job.failed or 0)
    m4b_queued = int(job.m4b_queued or 0)

    if result.get("needs_m4b"):
        m4b_queued += 1

    status = result.get("status") or ""
    retried = bool(result.get("retried"))
    if status == "completed":
        auto_applied += 1
        if retried:
            needs_review = max(0, needs_review - 1)
    elif status == "quarantined":
        if not retried:
            needs_review += 1
        # Seed review cursor on first quarantine if unset
        cursor = job.review_cursor_request_id
        if cursor is None and result.get("id"):
            await _update_job(job_id, review_cursor_request_id=int(result["id"]))
            job = await _load_job(job_id) or job
    elif status in ("failed", "admin_rejected", "cancelled"):
        failed += 1
    elif status in ("m4b_convert", "chapter_forge", "folder_forge", "finalizing", "metadata_forge"):
        # Still running somehow — count as scanned; forge should have finished sync.
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
