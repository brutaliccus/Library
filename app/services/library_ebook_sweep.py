"""Admin Ebook Sweep - backfill the ebook library through the DIY organizer.

Mirrors ``library_sweep.py`` (audiobook/ABS) but walks the ebook library on
disk instead of an ABS API, and drives ``ebook_pipeline.run_ebook_after_download``
(identify -> convert/embed -> organize -> Kavita) instead of LibraForge.
"""

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
from app.services import ebook_pipeline, kavita, library_ingest
from app.services import library_folder_cleanup as cleanup

logger = logging.getLogger(__name__)
settings = get_settings()

MEDIUM = "ebook"

_worker_task: asyncio.Task | None = None
_worker_lock = asyncio.Lock()

# Book the sweep worker is currently on.
_current: dict[str, Any] | None = None
# Next book folder the worker will actually process (skips already-done / unprocessed).
_up_next: dict[str, Any] | None = None
_skip_request_ids: set[int] = set()

# Completed sweep books since the last batched Kavita scan.
_completed_since_kavita_scan = 0
_kavita_scan_lock = asyncio.Lock()


def _kavita_scan_every() -> int:
    """How many completed books between full Kavita scans (min 1)."""
    raw = getattr(settings, "ebook_sweep_kavita_scan_every", 25)
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 25
    return max(1, n)


async def run_batched_kavita_scan(*, reason: str = "sweep") -> dict[str, Any]:
    """One full Kavita library scan (used by Ebook Sweep cadence)."""
    global _completed_since_kavita_scan
    async with _kavita_scan_lock:
        logger.info("Ebook Sweep Kavita batch scan (%s)", reason)
        try:
            status = await kavita.scan_library_and_wait(timeout_seconds=240)
            kavita.invalidate_cache()
            _completed_since_kavita_scan = 0
            return {"ok": True, "reason": reason, "scan": status}
        except Exception as e:
            logger.warning("Ebook Sweep Kavita batch scan failed (%s): %s", reason, e)
            return {"ok": False, "reason": reason, "error": str(e)[:300]}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rel_posix(root: Path, book_dir: Path) -> str:
    try:
        return book_dir.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return book_dir.as_posix()


def _folder_title_author_hint(root: Path, book_dir: Path) -> tuple[str, str | None]:
    """Best-effort title/author from the ``{author}/{series}/{title}`` layout."""
    try:
        rel_parts = book_dir.resolve().relative_to(root.resolve()).parts
    except (OSError, ValueError):
        rel_parts = (book_dir.name,)
    if not rel_parts:
        return book_dir.name, None
    title = rel_parts[-1]
    author = rel_parts[0] if len(rel_parts) >= 2 else None
    return title, author


def _set_current(
    *,
    request_id: int | None = None,
    title: str | None = None,
    author: str | None = None,
    cover_url: str | None = None,
    status: str | None = None,
    book_dir: str | None = None,
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
        "book_dir": book_dir,
    }


def _set_up_next(preview: dict[str, Any] | None) -> None:
    global _up_next
    _up_next = dict(preview) if preview else None


def _book_dir_preview(root: Path, book_dir: Path, *, applied: Any | None = None) -> dict[str, Any]:
    title, author = _folder_title_author_hint(root, book_dir)
    if applied is not None:
        title = applied.title or title
        author = applied.author or author
    return {
        "request_id": None,
        "title": title,
        "author": author,
        "cover_url": getattr(applied, "cover_url", None) if applied is not None else None,
        "book_dir": _rel_posix(root, book_dir),
    }


async def _dir_would_be_processed(
    root: Path, book_dir: Path, *, force_metadata: bool
) -> bool:
    """True when the sweep worker would kick the ebook pipeline for this folder."""
    from app.services.ebook_quick_review import load_applied_ebook_meta

    rel_posix = _rel_posix(root, book_dir)
    fingerprint = library_ingest.ebook_sweep_fingerprint(rel_posix)
    if await library_ingest.fingerprint_already_swept(fingerprint):
        return False
    if await library_ingest.fingerprint_in_flight(fingerprint):
        return False
    if not force_metadata and load_applied_ebook_meta(book_dir) is not None:
        return False
    existing = await library_ingest.latest_sweep_request(fingerprint)
    if not existing:
        return True
    if existing.status in library_ingest._SWEEP_WALK_SKIP_STATUSES:
        return False
    if existing.status in library_ingest._UNPROCESSED_STATUSES:
        return False
    return True


async def _peek_up_next(
    root: Path,
    dirs: list[Path],
    *,
    after_index: int,
    force_metadata: bool,
    lookahead: int = 250,
) -> dict[str, Any] | None:
    from app.services.ebook_quick_review import load_applied_ebook_meta

    end = min(len(dirs), max(after_index + 1, 0) + max(1, lookahead))
    for j in range(after_index + 1, end):
        book_dir = dirs[j]
        if await _dir_would_be_processed(root, book_dir, force_metadata=force_metadata):
            applied = load_applied_ebook_meta(book_dir)
            return _book_dir_preview(root, book_dir, applied=applied)
    return None


async def _refresh_current_from_db(request_id: int) -> None:
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
        "book_dir": (_current or {}).get("book_dir"),
    }


async def get_or_create_job() -> LibrarySweepJob:
    async with async_session() as db:
        result = await db.execute(
            select(LibrarySweepJob)
            .where(LibrarySweepJob.medium == MEDIUM)
            .order_by(LibrarySweepJob.id.desc())
            .limit(1)
        )
        job = result.scalar_one_or_none()
        if job:
            return job
        job = LibrarySweepJob(status="idle", medium=MEDIUM, updated_at=_utcnow())
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
        "ingest_fingerprint": r.ingest_fingerprint,
        "staging_path": r.staging_path,
        "cover_url": r.cover_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _request_to_processed(r: DownloadRequest) -> dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "author": r.author,
        "status": r.status,
        "status_detail": r.status_detail,
        "ingest_fingerprint": r.ingest_fingerprint,
        "staging_path": r.staging_path,
        "cover_url": r.cover_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


async def _unprocessed_counts() -> dict[str, int]:
    statuses = tuple(library_ingest._UNPROCESSED_STATUSES)
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest.status, func.count())
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.media_type == MEDIUM,
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
    """Prefer in-memory worker cursor; fall back to newest active sweep ebook row."""
    if _current and _current.get("request_id"):
        rid = int(_current["request_id"])
        await _refresh_current_from_db(rid)
        if _current:
            return dict(_current)
    if _current and _current.get("title"):
        return dict(_current)

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.media_type == MEDIUM,
                DownloadRequest.status.in_(
                    ("metadata_forge", "folder_forge", "finalizing")
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
        "book_dir": None,
    }


def job_to_dict(job: LibrarySweepJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "medium": job.medium,
        "status": job.status,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "total": int(job.total or 0),
        "scanned": int(job.scanned or 0),
        "auto_applied": int(job.auto_applied or 0),
        "needs_review": int(job.needs_review or 0),
        "failed": int(job.failed or 0),
        # Ebooks never use the M4B queue - always 0.
        "m4b_queued": 0,
        "review_cursor_request_id": job.review_cursor_request_id,
        "error": job.error,
        "started_by_user_id": job.started_by_user_id,
    }


async def get_status() -> dict[str, Any]:
    job = await get_or_create_job()
    data = job_to_dict(job)
    data["current"] = await _resolve_current_book()
    data["up_next"] = dict(_up_next) if _up_next else None
    data["unprocessed"] = await _unprocessed_counts()
    async with async_session() as db:
        data["processed_total"] = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(DownloadRequest)
                    .where(
                        DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                        DownloadRequest.media_type == MEDIUM,
                        DownloadRequest.status == "completed",
                    )
                )
            ).scalar_one()
            or 0
        )
    return data


async def set_review_cursor(request_id: int | None) -> dict[str, Any]:
    job = await get_or_create_job()
    await _update_job(job.id, review_cursor_request_id=request_id)
    return await get_status()


async def list_needs_review(*, limit: int = 200) -> list[dict[str, Any]]:
    """Quarantined DownloadRequests created by Ebook Sweep."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.media_type == MEDIUM,
                DownloadRequest.status == "quarantined",
            )
            .order_by(DownloadRequest.id.asc())
            .limit(max(1, min(limit, 1000)))
        )
        rows = list(result.scalars().all())
    return [_request_to_unprocessed(r) for r in rows]


async def list_unprocessed(*, limit: int = 500) -> list[dict[str, Any]]:
    """Cancelled / failed / skipped / admin_rejected sweep ebooks for manual reprocess."""
    statuses = tuple(library_ingest._UNPROCESSED_STATUSES)
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.media_type == MEDIUM,
                DownloadRequest.status.in_(statuses),
            )
            .order_by(DownloadRequest.id.desc())
            .limit(max(1, min(limit, 2000)))
        )
        rows = list(result.scalars().all())
    return [_request_to_unprocessed(r) for r in rows]


async def list_processed(*, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Successfully completed sweep ebooks (source=sweep, media_type=ebook, status=completed)."""
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    async with async_session() as db:
        total = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(DownloadRequest)
                    .where(
                        DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                        DownloadRequest.media_type == MEDIUM,
                        DownloadRequest.status == "completed",
                    )
                )
            ).scalar_one()
            or 0
        )
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.source == library_ingest.SOURCE_SWEEP,
                DownloadRequest.media_type == MEDIUM,
                DownloadRequest.status == "completed",
            )
            .order_by(
                DownloadRequest.completed_at.desc(),
                DownloadRequest.id.desc(),
            )
            .offset(off)
            .limit(lim)
        )
        rows = list(result.scalars().all())
    return {
        "items": [_request_to_processed(r) for r in rows],
        "total": total,
        "limit": lim,
        "offset": off,
        "count": len(rows),
    }


async def start_sweep(*, user_id: int) -> dict[str, Any]:
    """Start or resume an ebook sweep. Returns job status dict."""
    global _worker_task, _completed_since_kavita_scan
    job = await get_or_create_job()

    if job.status == "running":
        async with _worker_lock:
            if _worker_task is None or _worker_task.done():
                _worker_task = asyncio.create_task(
                    _run_sweep_worker(job.id), name="ebook-sweep"
                )
                return {**(await get_status()), "message": "Ebook Sweep worker restarted"}
        return {**(await get_status()), "message": "Ebook Sweep already running"}

    if job.status == "paused":
        await _update_job(
            job.id,
            status="running",
            error=None,
            started_by_user_id=user_id,
        )
        async with _worker_lock:
            if _worker_task is None or _worker_task.done():
                _worker_task = asyncio.create_task(
                    _run_sweep_worker(job.id), name="ebook-sweep"
                )
        return {**(await get_status()), "message": "Ebook Sweep resumed"}

    # Fresh start (idle / completed / cancelled)
    _skip_request_ids.clear()
    _set_current()
    _set_up_next(None)
    _completed_since_kavita_scan = 0
    await _update_job(
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
            _run_sweep_worker(job.id), name="ebook-sweep"
        )
    return {**(await get_status()), "message": "Ebook Sweep started"}


async def pause_sweep() -> dict[str, Any]:
    job = await get_or_create_job()
    if job.status != "running":
        return {**(await get_status()), "message": f"Cannot pause from status '{job.status}'"}
    await _update_job(job.id, status="paused")
    return {**(await get_status()), "message": "Ebook Sweep pausing after current book"}


async def cancel_sweep() -> dict[str, Any]:
    job = await get_or_create_job()
    if job.status not in ("running", "paused"):
        return {**(await get_status()), "message": f"Cannot cancel from status '{job.status}'"}
    was_paused = job.status == "paused"
    await _update_job(job.id, status="cancelled")
    _set_current()
    _set_up_next(None)
    if was_paused:
        await run_batched_kavita_scan(reason="sweep cancelled")
    return {**(await get_status()), "message": "Ebook Sweep cancelled"}


async def skip_current(*, request_id: int | None = None) -> dict[str, Any]:
    """Mark the current (or given) sweep ebook as skipped (aborts the in-flight pipeline)."""
    rid = request_id
    if rid is None and _current and _current.get("request_id"):
        rid = int(_current["request_id"])
    if rid is None:
        current = await _resolve_current_book()
        if current and current.get("request_id"):
            rid = int(current["request_id"])
    if rid is None:
        return {**(await get_status()), "message": "Nothing to skip", "skipped_id": None}

    _skip_request_ids.add(rid)
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == rid)
        )
        req = result.scalar_one_or_none()
        if not req:
            return {**(await get_status()), "message": "Request not found", "skipped_id": rid}
        if req.source != library_ingest.SOURCE_SWEEP or req.media_type != MEDIUM:
            return {
                **(await get_status()),
                "message": "Not an ebook sweep request",
                "skipped_id": rid,
            }
        # Ebook pipeline checks request status between stages (pipeline._is_cancelled
        # treats "skipped" as an abort signal) - no separate run to cancel.
        req.status = "skipped"
        req.status_detail = "Skipped during Ebook Sweep"
        req.progress_percent = None
        req.progress_bytes = None
        req.progress_total_bytes = None
        req.progress_speed_bps = None
        await db.commit()

    if _current and _current.get("request_id") == rid:
        _set_current()

    return {
        **(await get_status()),
        "message": f"Skipped request #{rid}",
        "skipped_id": rid,
    }


async def _request_is_skipped(request_id: int) -> bool:
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest.status).where(DownloadRequest.id == request_id)
        )
        return result.scalar_one_or_none() == "skipped"


async def _reprocess_local_ebook_ingest(request_id: int) -> dict[str, Any]:
    """Resolve (or restage) the ebook staging tree for a manual reprocess."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        if not library_ingest.is_local_ingest_request(req) or req.media_type != MEDIUM:
            raise ValueError("Not an ebook sweep/upload ingest request")
        if req.status in library_ingest._ACTIVE_FORGE:
            raise ValueError(f"Request already in progress ({req.status})")
        user_id = int(req.user_id)
        title = req.title or "Untitled"
        author = req.author
        staging_raw = (req.staging_path or "").strip()
        source_library_path = (req.source_library_path or "").strip() or None

    from app.services.forge_pipeline import resolve_staging_dir

    staging: Path | None = None
    if staging_raw:
        candidate = Path(staging_raw)
        if candidate.is_dir() and ebook_pipeline._collect_ebooks(candidate):
            staging = candidate
        else:
            try:
                resolved = resolve_staging_dir(staging_raw)
                if resolved.is_dir() and ebook_pipeline._collect_ebooks(resolved):
                    staging = resolved
            except FileNotFoundError:
                staging = None

    if staging is None and source_library_path:
        library_dir = Path(source_library_path)
        if library_dir.is_dir():
            staging = ebook_pipeline.ebook_staging_dir(request_id, title)
            library_ingest.stage_tree_from_library(library_dir, staging, prefer_hardlink=True)

    if staging is None:
        return {"ok": False, "id": request_id, "status": "failed", "reason": "staging_missing"}

    return {
        "ok": True,
        "id": request_id,
        "staging": staging,
        "user_id": user_id,
        "title": title,
        "author": author,
    }


async def reprocess_unprocessed(request_id: int, *, user_id: int) -> dict[str, Any]:
    """Re-kick the DIY ebook pipeline for a cancelled/failed/skipped/quarantined book.

    Restages synchronously, then runs the pipeline in a background task so the
    admin HTTP call returns immediately.
    """
    prepared = await _reprocess_local_ebook_ingest(request_id)
    if not prepared.get("ok", True):
        return {"ok": False, "result": prepared, "status": await get_status()}

    staging: Path = prepared["staging"]
    title = prepared["title"]
    author = prepared["author"]
    uid = prepared["user_id"]
    options = await library_ingest.ebook_sweep_options()

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if req:
            req.staging_path = ebook_pipeline.staging_path_for_storage(staging)
            req.status = "metadata_forge"
            req.status_detail = "Matching ebook metadata..."
            req.quarantine_reason = None
            await db.commit()

    async def _run() -> None:
        try:
            await ebook_pipeline.run_ebook_after_download(
                request_id,
                staging=staging,
                user_id=uid,
                title=title,
                author=author,
                resume_from="metadata",
                convert_all_to_epub=options["convert_all_to_epub"],
                force_metadata=True,
                provider_order=options["provider_order"],
            )
        except Exception:
            logger.exception("Background ebook reprocess failed for request %s", request_id)

    asyncio.create_task(_run(), name=f"ebook-sweep-reprocess-{request_id}")
    await _refresh_current_from_db(request_id)

    return {
        "ok": True,
        "result": {"ok": True, "id": request_id, "status": "metadata_forge"},
        "status": await get_status(),
    }


async def dismiss_unprocessed(request_ids: list[int] | int) -> dict[str, Any]:
    """Mark unprocessed sweep ebook(s) dismissed so they leave the Unprocessed tab.

    Does not delete library files - only the DownloadRequest status.
    """
    ids = [request_ids] if isinstance(request_ids, int) else list(request_ids)
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        raise ValueError("No request ids to dismiss")

    dismissed: list[int] = []
    skipped: list[dict[str, Any]] = []
    async with async_session() as db:
        for rid in ids:
            result = await db.execute(
                select(DownloadRequest).where(DownloadRequest.id == rid)
            )
            req = result.scalar_one_or_none()
            if not req:
                skipped.append({"id": rid, "reason": "not_found"})
                continue
            if (req.source or "").strip().lower() != library_ingest.SOURCE_SWEEP or req.media_type != MEDIUM:
                skipped.append({"id": rid, "reason": "not_ebook_sweep"})
                continue
            if req.status not in library_ingest._UNPROCESSED_STATUSES:
                skipped.append({"id": rid, "reason": f"status_{req.status}"})
                continue
            req.status = library_ingest.STATUS_SWEEP_DISMISSED
            req.status_detail = "Removed from Unprocessed queue"
            dismissed.append(rid)
        await db.commit()

    return {
        "ok": True,
        "dismissed": dismissed,
        "skipped": skipped,
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
        logger.info("Ebook Sweep: restarting worker for job %s after startup", job.id)
        _worker_task = asyncio.create_task(
            _run_sweep_worker(job.id), name="ebook-sweep"
        )


async def _enumerate_ebook_books() -> list[Path]:
    """Book folders under the ebook library root (staging excluded)."""
    root = Path(settings.ebook_dir)
    if not root.is_dir():
        return []
    try:
        dirs = sorted(cleanup.iter_ebook_book_dirs(root))
    except Exception:
        logger.warning("Ebook Sweep: failed to enumerate book folders", exc_info=True)
        return []
    return dirs


async def _retry_quarantined_ebook(
    existing: DownloadRequest,
    *,
    user_id: int,
    title: str,
    author: str | None,
    book_dir: Path,
) -> dict[str, Any]:
    """Re-kick the ebook pipeline for an existing quarantined sweep request."""
    from app.services.forge_pipeline import resolve_staging_dir

    staging: Path | None = None
    staging_raw = (existing.staging_path or "").strip()
    if staging_raw:
        try:
            candidate = resolve_staging_dir(staging_raw)
            if candidate.is_dir() and ebook_pipeline._collect_ebooks(candidate):
                staging = candidate
        except FileNotFoundError:
            staging = None

    if staging is None:
        # Staging was already wiped (or missing) - the source folder is still
        # present in the library since it hasn't been organized yet.
        staging = ebook_pipeline.ebook_staging_dir(existing.id, title)
        library_ingest.stage_tree_from_library(book_dir, staging, prefer_hardlink=True)

    options = await library_ingest.ebook_sweep_options()
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == existing.id)
        )
        req = result.scalar_one_or_none()
        if req:
            req.staging_path = ebook_pipeline.staging_path_for_storage(staging)
            req.status = "metadata_forge"
            req.status_detail = "Retrying ebook metadata..."
            req.quarantine_reason = None
            await db.commit()

    await ebook_pipeline.run_ebook_after_download(
        existing.id,
        staging=staging,
        user_id=user_id,
        title=title,
        author=author,
        resume_from="metadata",
        convert_all_to_epub=options["convert_all_to_epub"],
        force_metadata=True,
        provider_order=options["provider_order"],
    )

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == existing.id)
        )
        req = result.scalar_one_or_none()
        status = req.status if req else "unknown"

    return {"ok": True, "id": existing.id, "status": status, "retried": True}


async def _run_sweep_worker(job_id: int) -> None:
    try:
        root = Path(settings.ebook_dir)
        options = await library_ingest.ebook_sweep_options()
        force_metadata = bool(options["force_metadata"])
        dirs = await _enumerate_ebook_books()
        await _update_job(job_id, total=len(dirs))
        logger.info("Ebook Sweep job %s: %s book folders", job_id, len(dirs))

        for index, book_dir in enumerate(dirs):
            job = await _load_job(job_id)
            if not job:
                return
            if job.status == "paused":
                logger.info("Ebook Sweep job %s paused at index %s", job_id, index)
                _set_current()
                _set_up_next(
                    await _peek_up_next(
                        root, dirs, after_index=index - 1, force_metadata=force_metadata
                    )
                )
                await run_batched_kavita_scan(reason="sweep paused")
                return
            if job.status == "cancelled":
                logger.info("Ebook Sweep job %s cancelled at index %s", job_id, index)
                _set_current()
                _set_up_next(None)
                await run_batched_kavita_scan(reason="sweep cancelled")
                return
            if job.status != "running":
                _set_current()
                _set_up_next(None)
                await run_batched_kavita_scan(reason=f"sweep stopped ({job.status})")
                return

            _set_up_next(
                await _peek_up_next(
                    root, dirs, after_index=index, force_metadata=force_metadata
                )
            )

            try:
                await _process_one_dir(job_id, root, book_dir, scan_index=index, options=options)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Ebook Sweep item failed: %s", e)
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
            logger.info("Ebook Sweep job %s completed", job_id)
            await run_batched_kavita_scan(reason="sweep completed")
        _set_current()
        _set_up_next(None)
    except asyncio.CancelledError:
        logger.info("Ebook Sweep worker cancelled")
        _set_current()
        _set_up_next(None)
        await run_batched_kavita_scan(reason="sweep stopped")
        raise
    except Exception as e:
        logger.exception("Ebook Sweep worker crashed: %s", e)
        job = await _load_job(job_id)
        if job and job.status == "running":
            await _update_job(job_id, status="completed", error=str(e)[:500])
        _set_current()
        _set_up_next(None)
        await run_batched_kavita_scan(reason="sweep error stop")


async def _process_one_dir(
    job_id: int,
    root: Path,
    book_dir: Path,
    *,
    scan_index: int,
    options: dict[str, Any],
) -> None:
    from app.services.ebook_quick_review import load_applied_ebook_meta

    rel_posix = _rel_posix(root, book_dir)
    fingerprint = library_ingest.ebook_sweep_fingerprint(rel_posix)
    applied = load_applied_ebook_meta(book_dir)
    title, author = _folder_title_author_hint(root, book_dir)
    if applied is not None:
        title = applied.title or title
        author = applied.author or author

    _set_current(
        request_id=None,
        title=title,
        author=author,
        cover_url=getattr(applied, "cover_url", None) if applied is not None else None,
        status="scanning",
        book_dir=rel_posix,
    )

    if await library_ingest.fingerprint_already_swept(fingerprint):
        await _update_job(job_id, scanned=scan_index + 1)
        return

    if await library_ingest.fingerprint_in_flight(fingerprint):
        await _update_job(job_id, scanned=scan_index + 1)
        return

    if not options["force_metadata"] and applied is not None:
        # Already organized (has ebook_applied.json) and force-metadata is off.
        await _update_job(job_id, scanned=scan_index + 1)
        return

    job = await _load_job(job_id)
    if not job or not job.started_by_user_id:
        raise RuntimeError("Ebook Sweep job missing started_by_user_id")
    user_id = int(job.started_by_user_id)

    existing = await library_ingest.latest_sweep_request(fingerprint)
    if existing and existing.status == "quarantined":
        _set_current(
            request_id=existing.id,
            title=existing.title or title,
            author=existing.author or author,
            cover_url=existing.cover_url,
            status=existing.status,
            book_dir=rel_posix,
        )
        result = await _retry_quarantined_ebook(
            existing, user_id=user_id, title=title, author=author, book_dir=book_dir
        )
        await _refresh_current_from_db(existing.id)
        await _record_sweep_result(job_id, result, scan_index=scan_index)
        return

    if existing and existing.status in library_ingest._SWEEP_WALK_SKIP_STATUSES:
        await _update_job(job_id, scanned=scan_index + 1)
        return

    if existing and existing.status in library_ingest._UNPROCESSED_STATUSES:
        await _update_job(job_id, scanned=scan_index + 1)
        logger.debug(
            "Ebook Sweep skip unprocessed fingerprint %s (status=%s)",
            fingerprint,
            existing.status,
        )
        return

    async def _on_created(rid: int) -> None:
        _set_current(
            request_id=rid,
            title=title,
            author=author,
            cover_url=None,
            status="metadata_forge",
            book_dir=rel_posix,
        )

    result = await library_ingest.ingest_ebook_from_library_folder(
        user_id=user_id,
        title=title,
        author=author,
        library_dir=book_dir,
        magnet_link=library_ingest.ebook_sweep_magnet(rel_posix),
        ingest_fingerprint=fingerprint,
        cover_url=None,
        convert_all_to_epub=options["convert_all_to_epub"],
        force_metadata=options["force_metadata"],
        provider_order=options["provider_order"],
        kick_pipeline=True,
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


async def _record_sweep_result(
    job_id: int, result: dict[str, Any], *, scan_index: int
) -> None:
    global _completed_since_kavita_scan
    job = await _load_job(job_id)
    if not job:
        return

    scanned = scan_index + 1
    auto_applied = int(job.auto_applied or 0)
    needs_review = int(job.needs_review or 0)
    failed = int(job.failed or 0)

    status = result.get("status") or ""
    retried = bool(result.get("retried"))

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
    elif status in ("metadata_forge", "folder_forge", "finalizing"):
        # Sync path finished mid-pipeline somehow.
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
        error=None,
    )

    _completed_since_kavita_scan += 1
    every = _kavita_scan_every()
    if _completed_since_kavita_scan >= every:
        await run_batched_kavita_scan(
            reason=f"every {every} completed (at {_completed_since_kavita_scan})"
        )
