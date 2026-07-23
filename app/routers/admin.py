import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ABSPlayTracking,
    AvailabilityAlert,
    DownloadRequest,
    LibraryGroup,
    PushSubscription,
    SearchHistory,
    StreamHistory,
    StreamingLibraryItem,
    User,
)
from app.utils.auth import require_admin, hash_password
from app.services import real_debrid, audiobookshelf, kavita, downloader, goodreads
from app.services.pipeline import (
    audiobook_destination_dir,
    organize_audiobook_files,
    _is_collection_title,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)

# Client heartbeats ~every 60s while focused; treat as online within ~3 minutes.
ONLINE_THRESHOLD = timedelta(minutes=3)


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: str
    library_name: str | None = None
    last_seen_at: str | None = None
    is_online: bool = False
    requests_total: int = 0
    stream_sessions: int = 0
    last_stream_at: str | None = None
    abs_titles_played: int = 0
    last_abs_played_at: str | None = None
    active_alerts: int = 0
    finished_streams: int = 0

    model_config = {"from_attributes": True}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def later_datetime(*values: datetime | None) -> datetime | None:
    """Return the latest non-None timestamp (naive treated as UTC)."""
    best: datetime | None = None
    for raw in values:
        dt = _as_utc(raw)
        if dt is None:
            continue
        if best is None or dt > best:
            best = dt
    return best


def user_is_online(last_seen_at: datetime | None, *, now: datetime | None = None) -> bool:
    """True when last_seen_at is within ONLINE_THRESHOLD of now."""
    if last_seen_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    seen = _as_utc(last_seen_at)
    assert seen is not None
    return (now - seen) <= ONLINE_THRESHOLD


class SetActiveBody(BaseModel):
    is_active: bool


class AdminDownloadResponse(BaseModel):
    id: int
    title: str
    author: str | None
    media_type: str = "audiobook"
    status: str
    status_detail: str | None
    username: str
    is_private: bool = False
    google_volume_id: str | None = None
    cover_url: str | None = None
    size_bytes: int | None = None
    indexer: str | None = None
    created_at: str
    completed_at: str | None
    progress_percent: float | None = None
    progress_bytes: int | None = None
    progress_total_bytes: int | None = None
    progress_speed_bps: float | None = None
    staging_path: str | None = None
    quarantine_reason: str | None = None
    manual_review_url: str | None = None


class RejectRequestBody(BaseModel):
    reason: str = "Rejected by admin"
    delete_files: bool = True


class StagingFileDeleteBody(BaseModel):
    """Relative path inside the request staging tree (POSIX, no ..)."""
    path: str


# --- User Management ---

async def _get_user_or_404(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _ensure_not_last_active_admin(
    db: AsyncSession,
    user: User,
    *,
    action: str,
) -> None:
    """Block disabling/deleting an admin when no other active admin would remain."""
    if user.role != "admin":
        return
    # Disabling an already-disabled account is a no-op path; skip.
    if action == "disable" and not user.is_active:
        return
    other = (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin")
            .where(User.is_active.is_(True))
            .where(User.id != user.id)
        )
    ).scalar_one()
    if int(other or 0) < 1:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot {action} the last active admin",
        )


async def _ensure_can_remove_library_owner(db: AsyncSession, user: User) -> None:
    """Owners can't be deleted while other library members remain (same as leave)."""
    owned = (
        await db.execute(select(LibraryGroup).where(LibraryGroup.owner_user_id == user.id))
    ).scalars().all()
    for group in owned:
        others = (
            await db.execute(
                select(User.id)
                .where(User.library_group_id == group.id)
                .where(User.id != user.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if others is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "User owns a library with other members. "
                    "Promote a new owner or remove members first."
                ),
            )


async def _delete_user_related_rows(db: AsyncSession, user_id: int) -> None:
    """Remove FK-dependent rows before hard-deleting a user (no ON DELETE CASCADE)."""
    for model in (
        DownloadRequest,
        SearchHistory,
        StreamHistory,
        PushSubscription,
        ABSPlayTracking,
        StreamingLibraryItem,
        AvailabilityAlert,
    ):
        await db.execute(delete(model).where(model.user_id == user_id))


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List accounts with per-user activity stats (aggregated — no N+1)."""
    rows = (
        await db.execute(
            select(User, LibraryGroup.name)
            .outerjoin(LibraryGroup, User.library_group_id == LibraryGroup.id)
            .order_by(User.created_at.desc())
        )
    ).all()

    req_counts = dict(
        (
            await db.execute(
                select(DownloadRequest.user_id, func.count())
                .group_by(DownloadRequest.user_id)
            )
        ).all()
    )
    stream_rows = (
        await db.execute(
            select(
                StreamHistory.user_id,
                func.count().label("sessions"),
                func.coalesce(
                    func.sum(case((StreamHistory.status == "finished", 1), else_=0)),
                    0,
                ).label("finished"),
                func.max(StreamHistory.updated_at).label("last_stream"),
            ).group_by(StreamHistory.user_id)
        )
    ).all()
    stream_stats = {
        uid: {
            "sessions": int(sessions or 0),
            "finished": int(finished or 0),
            "last_stream": last_stream,
        }
        for uid, sessions, finished, last_stream in stream_rows
    }
    abs_rows = (
        await db.execute(
            select(
                ABSPlayTracking.user_id,
                func.count().label("titles"),
                func.max(ABSPlayTracking.last_played_at).label("last_played"),
            ).group_by(ABSPlayTracking.user_id)
        )
    ).all()
    abs_stats = {
        uid: {"titles": int(titles or 0), "last_played": last_played}
        for uid, titles, last_played in abs_rows
    }
    alert_counts = dict(
        (
            await db.execute(
                select(AvailabilityAlert.user_id, func.count())
                .where(AvailabilityAlert.notified_at.is_(None))
                .group_by(AvailabilityAlert.user_id)
            )
        ).all()
    )

    now = datetime.now(timezone.utc)
    out: list[UserResponse] = []
    for user, library_name in rows:
        st = stream_stats.get(user.id, {})
        ab = abs_stats.get(user.id, {})
        rd_sessions = int(st.get("sessions") or 0)
        abs_titles = int(ab.get("titles") or 0)
        # Listening activity spans debrid stream_history AND ABS library playback.
        # ABS never writes stream_history (progress goes to ABS + abs_play_tracking).
        out.append(
            UserResponse(
                id=user.id,
                username=user.username,
                role=user.role,
                is_active=user.is_active,
                created_at=_iso(user.created_at) or "",
                library_name=library_name,
                last_seen_at=_iso(user.last_seen_at),
                is_online=user_is_online(user.last_seen_at, now=now),
                requests_total=int(req_counts.get(user.id) or 0),
                stream_sessions=rd_sessions + abs_titles,
                last_stream_at=_iso(
                    later_datetime(st.get("last_stream"), ab.get("last_played"))
                ),
                abs_titles_played=abs_titles,
                last_abs_played_at=_iso(ab.get("last_played")),
                active_alerts=int(alert_counts.get(user.id) or 0),
                finished_streams=int(st.get("finished") or 0),
            )
        )
    return out


@router.patch("/users/{user_id}")
async def set_user_active(
    user_id: int,
    body: SetActiveBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable an account (soft toggle via is_active)."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot change your own active status")

    user = await _get_user_or_404(db, user_id)
    if body.is_active == user.is_active:
        state = "enabled" if user.is_active else "disabled"
        return {"message": f"User {user.username} is already {state}", "is_active": user.is_active}

    if not body.is_active:
        await _ensure_not_last_active_admin(db, user, action="disable")

    user.is_active = body.is_active
    await db.commit()
    state = "enabled" if user.is_active else "disabled"
    return {"message": f"User {user.username} {state}", "is_active": user.is_active}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a user account and related per-user rows."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    user = await _get_user_or_404(db, user_id)
    await _ensure_not_last_active_admin(db, user, action="delete")
    await _ensure_can_remove_library_owner(db, user)

    username = user.username
    owned_group_ids = [
        g.id
        for g in (
            await db.execute(select(LibraryGroup).where(LibraryGroup.owner_user_id == user.id))
        ).scalars().all()
    ]

    await _delete_user_related_rows(db, user_id)

    # Break circular FK: clear membership, drop empty owned groups, then user.
    user.library_group_id = None
    user.library_role = "member"
    await db.flush()

    for group_id in owned_group_ids:
        remaining = (
            await db.execute(
                select(User.id).where(User.library_group_id == group_id).limit(1)
            )
        ).scalar_one_or_none()
        if remaining is None:
            group = (
                await db.execute(select(LibraryGroup).where(LibraryGroup.id == group_id))
            ).scalar_one_or_none()
            if group:
                await db.delete(group)
                await db.flush()

    await db.delete(user)
    await db.commit()
    return {"message": f"User {username} deleted"}


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_or_404(db, user_id)

    default_password = "changeme"
    user.hashed_password = hash_password(default_password)
    user.must_change_password = True
    await db.commit()

    return {"message": f"Password reset for {user.username} (default password: changeme)"}


# --- All Requests ---

@router.get("/download-requests", response_model=list[AdminDownloadResponse])
async def list_all_downloads(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services import libraforge

    result = await db.execute(
        select(DownloadRequest, User.username)
        .join(User, DownloadRequest.user_id == User.id)
        .order_by(DownloadRequest.created_at.desc())
    )
    review = libraforge.public_manual_review_url() or None
    return [
        AdminDownloadResponse(
            id=req.id,
            title=req.title,
            author=req.author,
            media_type=req.media_type or "unknown",
            status=req.status,
            status_detail=req.status_detail,
            username=username,
            is_private=bool(req.is_private),
            google_volume_id=getattr(req, "google_volume_id", None),
            cover_url=getattr(req, "cover_url", None),
            size_bytes=req.size_bytes,
            indexer=req.indexer,
            created_at=req.created_at.isoformat() if req.created_at else "",
            completed_at=req.completed_at.isoformat() if req.completed_at else None,
            progress_percent=req.progress_percent,
            progress_bytes=req.progress_bytes,
            progress_total_bytes=req.progress_total_bytes,
            progress_speed_bps=req.progress_speed_bps,
            staging_path=getattr(req, "staging_path", None),
            quarantine_reason=getattr(req, "quarantine_reason", None),
            manual_review_url=review if req.status == "quarantined" else None,
        )
        for req, username in result.all()
    ]


@router.post("/download-requests/{request_id}/reject")
async def reject_download_request(
    request_id: int,
    body: RejectRequestBody = Body(default_factory=RejectRequestBody),
    _admin: User = Depends(require_admin),
):
    """Reject a quarantined (or failed) request — user sees admin-rejected like a failure."""
    from app.services.forge_pipeline import reject_quarantined_request

    try:
        req = await reject_quarantined_request(
            request_id,
            delete_files=body.delete_files,
            reason=body.reason or "Rejected by admin",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": True,
        "id": req.id,
        "status": req.status,
        "status_detail": req.status_detail,
    }


@router.post("/download-requests/{request_id}/continue-forge")
async def continue_forge_after_review(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """After Manual Review in LibraForge, resume M4B → Folder Forge → ABS."""
    from app.services.forge_pipeline import continue_after_manual_review
    from app.services.pipeline import _update_status

    result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status not in ("quarantined", "metadata_forge"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot continue request in status '{req.status}'",
        )
    if not (req.staging_path or "").strip():
        raise HTTPException(status_code=400, detail="Request has no staging_path")

    # Flip out of quarantined before returning so Admin/My Requests refetch
    # immediately sees progress (background task may take a moment to start).
    req.quarantine_reason = None
    await db.commit()
    await _update_status(
        db,
        request_id,
        "m4b_convert",
        "Resuming after manual review…",
    )

    asyncio.create_task(continue_after_manual_review(request_id))
    return {
        "ok": True,
        "id": request_id,
        "status": "m4b_convert",
        "message": "Continuing LibraForge pipeline",
    }


async def _staging_request_or_404(db: AsyncSession, request_id: int) -> DownloadRequest:
    result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if not (req.staging_path or "").strip():
        raise HTTPException(status_code=400, detail="Request has no staging_path")
    return req


@router.get("/requests/{request_id}/staging-files")
@router.get("/download-requests/{request_id}/staging-files")
async def list_request_staging_files(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List the request's `_unorganized` staging tree (admin file browser)."""
    from app.services.forge_pipeline import build_staging_tree, resolve_staging_dir

    req = await _staging_request_or_404(db, request_id)
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    tree = build_staging_tree(staging)
    return {
        "request_id": request_id,
        "title": req.title,
        "status": req.status,
        **tree,
    }


@router.delete("/requests/{request_id}/staging-files")
@router.delete("/download-requests/{request_id}/staging-files")
async def delete_request_staging_file(
    request_id: int,
    body: StagingFileDeleteBody,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete one file (or empty dir) under the request staging tree."""
    from app.services.forge_pipeline import delete_staging_entry, resolve_staging_dir

    req = await _staging_request_or_404(db, request_id)
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        result = delete_staging_entry(staging, body.path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "Admin %s deleted staging entry for request %s: %s",
        _admin.username,
        request_id,
        body.path,
    )
    return result


@router.post("/download-requests/{request_id}/reorganize")
async def reorganize_audiobook_download(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Re-run chapter flatten / collection split for an audiobook already on disk, then trigger ABS scan."""
    result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if (req.media_type or "audiobook") != "audiobook":
        raise HTTPException(status_code=400, detail="Only audiobook requests can be reorganized")

    author, book_title = downloader.parse_torrent_name(req.title)
    if req.author:
        author = req.author
        if book_title == author or not book_title or book_title == "Unknown":
            stripped = re.sub(r"\s*-\s*" + re.escape(author) + r"\s*$", "", req.title, flags=re.IGNORECASE).strip()
            book_title = downloader.sanitize_filename(stripped) if stripped else downloader.sanitize_filename(req.title)

    dest = audiobook_destination_dir(request_id, author, book_title)
    if not dest.is_dir():
        raise HTTPException(status_code=404, detail=f"Audiobook folder not found on disk: {dest}")

    series_override = None
    if _is_collection_title(book_title):
        first_book = re.sub(
            r"\s*(?:Books?|Vol(?:ume)?s?)\s*1\s*[-–]\s*\d+\s*$", "", book_title, flags=re.IGNORECASE
        ).strip()
        if first_book:
            try:
                series_override = await goodreads.get_series(first_book, author)
            except Exception as e:
                logger.debug("Goodreads series lookup on reorganize failed: %s", e)

    try:
        book_dirs = organize_audiobook_files(dest, author, series_override=series_override)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    try:
        await audiobookshelf.scan_library()
        await asyncio.sleep(5)
        await audiobookshelf.remove_items_with_issues()
    except Exception as e:
        logger.warning("ABS scan after reorganize failed: %s", e)

    audiobookshelf.invalidate_cache()
    return {"ok": True, "book_dirs": [str(p.resolve()) for p in book_dirs]}


# --- ABS Metadata ---

@router.post("/abs/fix-metadata")
async def fix_abs_metadata(_admin: User = Depends(require_admin)):
    """Scan ABS, purge missing/orphan items, then align titles with folder names where they differ."""
    result = await audiobookshelf.fix_metadata_mismatches()
    if result.get("fetch_error"):
        raise HTTPException(status_code=502, detail=result["fetch_error"])
    return result


@router.post("/abs/rematch/{item_id}")
async def rematch_abs_item(
    item_id: str,
    _admin: User = Depends(require_admin),
):
    """Trigger ABS quick match for a single library item."""
    result = await audiobookshelf.match_item(item_id)
    if result is None:
        raise HTTPException(status_code=502, detail="Failed to match item in ABS")
    return {"updated": result.get("updated", False)}


# --- System Health ---

@router.get("/health")
async def system_health(_admin: User = Depends(require_admin)):
    from app.services.health_checks import collect_system_health

    return await collect_system_health()


@router.get("/libraforge")
async def libraforge_status(_admin: User = Depends(require_admin)):
    """Admin deep-link status for sibling LibraForge (no proxy)."""
    from app.config import get_settings
    from app.services.health_checks import _probe_libraforge

    settings = get_settings()
    probe = await _probe_libraforge()
    url = (settings.libraforge_url or "").strip() or None
    return {
        "url": url,
        "configured": bool(url) or bool(probe.get("configured")),
        "connected": bool(probe.get("connected")),
        "error": probe.get("error"),
    }


@router.get("/kavita-debug")
async def kavita_debug(_admin: User = Depends(require_admin)):
    """Diagnostic endpoint for Kavita ebook loading. Returns raw API response and errors."""
    from app.config import get_settings
    import httpx

    settings = get_settings()
    result = {
        "kavita_url": settings.kavita_url,
        "api_key_set": bool(settings.kavita_api_key),
        "library_id": settings.kavita_library_id,
        "health_ok": False,
        "series_api_ok": False,
        "series_count": 0,
        "ebook_count": 0,
        "error": None,
        "raw_sample": None,
    }

    # Health (no auth)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{settings.kavita_url}/api/health", timeout=5)
            result["health_ok"] = r.status_code == 200
    except Exception as e:
        result["error"] = f"Health check failed: {e}"
        return result

    if not settings.kavita_api_key:
        result["error"] = "KAVITA_API_KEY not set in .env"
        return result

    # Series all-v2 (requires auth)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{settings.kavita_url}/api/Series/all-v2",
                headers={"x-api-key": settings.kavita_api_key, "Content-Type": "application/json"},
                json={},
                params={"PageSize": 0},
                timeout=30,
            )
            result["series_api_ok"] = r.status_code == 200
            if r.status_code != 200:
                result["error"] = f"Series API returned {r.status_code}: {r.text[:500]}"
                return result
            data = r.json()
            items = data if isinstance(data, list) else (data.get("items", []) if isinstance(data, dict) else [])
            result["series_count"] = len(items)
            result["ebook_count"] = sum(1 for s in items if s.get("format") in (3, 4))
            result["raw_sample"] = items[:2] if items else None
    except Exception as e:
        result["error"] = str(e)
    return result


class ScraperEnabledRequest(BaseModel):
    enabled: bool


@router.get("/scraper-status")
async def scraper_status(_admin: User = Depends(require_admin)):
    from app.services import indexer_scraper
    return await indexer_scraper.get_status()


@router.post("/scraper-enabled")
async def scraper_set_enabled(
    body: ScraperEnabledRequest,
    _admin: User = Depends(require_admin),
):
    from app.services import indexer_scraper
    await indexer_scraper.set_enabled(body.enabled)
    return await indexer_scraper.get_status()


@router.post("/scraper-run-now")
async def scraper_run_now(_admin: User = Depends(require_admin)):
    from app.services import indexer_scraper
    result = await indexer_scraper.trigger_scrape_now()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "Cannot run scraper now"))
    return result["status"]


@router.post("/scraper-clear-error")
async def scraper_clear_error(_admin: User = Depends(require_admin)):
    """Clear scraper last_error and dismiss failed debrid-rescan / catalog-relink banners."""
    from app.services import indexer_scraper
    await indexer_scraper.clear_error()
    await indexer_scraper.clear_job_errors(force_stop=False)
    return await indexer_scraper.get_status()


@router.post("/scraper-clear-job-errors")
async def scraper_clear_job_errors(
    force_stop: bool = False,
    _admin: User = Depends(require_admin),
):
    """Dismiss debrid/relink error banners. force_stop=true also marks stuck runs idle."""
    from app.services import indexer_scraper
    result = await indexer_scraper.clear_job_errors(force_stop=force_stop)
    status = await indexer_scraper.get_status()
    return {**result, "status": status}


@router.post("/scraper-refresh-debrid")
async def scraper_refresh_debrid(_admin: User = Depends(require_admin)):
    """Re-probe Torbox/RD instant flags for cached torrents."""
    from app.services import indexer_scraper
    result = await indexer_scraper.refresh_debrid_cache()
    status = await indexer_scraper.get_status()
    return {**result, "status": status}


@router.post("/scraper-rescan-all-debrid")
async def scraper_rescan_all_debrid(_admin: User = Depends(require_admin)):
    """Re-queue every cached torrent for debrid cache checks and catalog preload."""
    from app.services import indexer_scraper
    return await indexer_scraper.start_full_debrid_rescan()


class CatalogRelinkRequest(BaseModel):
    # When true (default), deactivate book torrents that match no catalog entry
    # after re-linking (miscategorised non-book noise).
    prune_unmatched: bool = True


@router.post("/scraper-relink-catalog")
async def scraper_relink_catalog(
    body: CatalogRelinkRequest | None = None,
    _admin: User = Depends(require_admin),
):
    """Re-link every cached torrent against the local Open Library catalog and
    prune entries that match nothing (backfill after the OL ban)."""
    from app.services import indexer_scraper
    prune = body.prune_unmatched if body else True
    return await indexer_scraper.start_catalog_relink(prune_unmatched=prune)


class ScraperSettingsUpdate(BaseModel):
    # Partial update: any subset of the fields declared in scraper_settings.FIELDS.
    updates: dict[str, int | str | bool]


def _scraper_settings_payload(cfg) -> dict:
    from app.services import scraper_settings
    return {
        "settings": scraper_settings.config_as_dict(cfg),
        "defaults": scraper_settings.env_defaults(),
        "fields": scraper_settings.field_descriptors(),
    }


@router.get("/scraper-settings")
async def get_scraper_settings(_admin: User = Depends(require_admin)):
    from app.services import scraper_settings
    cfg = await scraper_settings.get_scraper_config()
    return _scraper_settings_payload(cfg)


@router.put("/scraper-settings")
async def update_scraper_settings(
    body: ScraperSettingsUpdate,
    _admin: User = Depends(require_admin),
):
    from app.services import scraper_settings
    try:
        cfg = await scraper_settings.update_scraper_config(body.updates)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _scraper_settings_payload(cfg)


@router.post("/scraper-settings/reset")
async def reset_scraper_settings(_admin: User = Depends(require_admin)):
    from app.services import scraper_settings
    cfg = await scraper_settings.reset_scraper_config()
    return _scraper_settings_payload(cfg)


class IntegrationKeysUpdate(BaseModel):
    # Only fields that are present are updated. Send "" to clear a key.
    nyt_api_key: str | None = None
    isbndb_api_key: str | None = None
    hardcover_api_key: str | None = None
    mullvad_account_number: str | None = None


def _mask(secret: str) -> str:
    """Show only the last 4 chars so admins can confirm which key is stored."""
    if not secret:
        return ""
    return ("*" * max(0, len(secret) - 4)) + secret[-4:]


MULLVAD_SETTING = "integrations.mullvad_account_number"
_MULLVAD_ENV_PATH = Path("/app/data/mullvad.env")


def _normalize_mullvad_account(raw: str) -> str:
    """Strip spaces/dashes — Mullvad account numbers are 16 digits."""
    return re.sub(r"\D", "", (raw or "").strip())


def _write_mullvad_env_file(account: str, *, private_key: str = "", addresses: str = "") -> None:
    """Keep gluetun env in sync under ./data (bind-mounted). Restart gluetun to apply."""
    try:
        _MULLVAD_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        if private_key and addresses:
            from app.services.mullvad import write_gluetun_env

            write_gluetun_env(
                str(_MULLVAD_ENV_PATH),
                private_key=private_key,
                addresses=addresses,
                account=account,
            )
        elif account:
            # Account alone is not enough for WireGuard — keys required.
            _MULLVAD_ENV_PATH.write_text(
                f"MULLVAD_ACCOUNT_NUMBER={account}\n", encoding="utf-8"
            )
        elif _MULLVAD_ENV_PATH.exists():
            _MULLVAD_ENV_PATH.unlink()
    except Exception as e:  # pragma: no cover
        logger.warning("Failed to write mullvad.env: %s", e)


async def _resolve_mullvad_account() -> tuple[str, str]:
    """Return (stored_override, effective) Mullvad account digits."""
    from app.config import get_settings
    from app.services import app_settings

    env_key = _normalize_mullvad_account(get_settings().mullvad_account_number or "")
    stored = _normalize_mullvad_account(
        await app_settings.get_setting(MULLVAD_SETTING, default="")
    )
    return stored, stored or env_key


async def _integrations_payload() -> dict:
    from app.services import nyt_books, isbndb, hardcover, app_settings

    stored = await app_settings.get_setting(nyt_books.API_KEY_SETTING, default="")
    effective = await nyt_books.get_api_key()
    isbn_stored = await app_settings.get_setting(isbndb.API_KEY_SETTING, default="")
    isbn_effective = await isbndb.get_api_key()
    hc_stored = await app_settings.get_setting(hardcover.API_KEY_SETTING, default="")
    hc_effective = await hardcover.get_api_key()
    mullvad_stored, mullvad_eff = await _resolve_mullvad_account()
    wg_key = await app_settings.get_setting("integrations.mullvad_wg_private_key", default="")
    wg_addr = await app_settings.get_setting("integrations.mullvad_wg_addresses", default="")
    return {
        "nyt": {
            "configured": bool(effective),
            # True when the key comes from the admin override (not just env).
            "overridden": bool(stored),
            "hint": _mask(effective),
        },
        "isbndb": {
            "configured": bool(isbn_effective),
            "overridden": bool(isbn_stored),
            "hint": _mask(isbn_effective),
        },
        "hardcover": {
            "configured": bool(hc_effective),
            "overridden": bool(hc_stored),
            "hint": _mask(hc_effective.replace("Bearer ", "") if hc_effective else ""),
        },
        "mullvad": {
            "configured": bool(mullvad_eff),
            "overridden": bool(mullvad_stored),
            "hint": _mask(mullvad_eff),
            "wireguardReady": bool(wg_key and wg_addr),
            "wireguardHint": _mask(wg_addr) if wg_addr else "",
            "note": "Only ABB traffic uses Mullvad (FlareSolverr → gluetun:8888). "
                    "Jackett/Knaben/Prowlarr stay on your LAN. Saving an account "
                    "auto-registers WireGuard keys into data/mullvad.env — then "
                    "restart: docker compose up -d gluetun",
        },
    }


@router.get("/integrations")
async def get_integrations(_admin: User = Depends(require_admin)):
    return await _integrations_payload()


@router.put("/integrations")
async def update_integrations(
    body: IntegrationKeysUpdate,
    _admin: User = Depends(require_admin),
):
    from app.services import nyt_books, isbndb, hardcover, app_settings

    if body.nyt_api_key is not None:
        await app_settings.set_setting(nyt_books.API_KEY_SETTING, body.nyt_api_key.strip())
    if body.isbndb_api_key is not None:
        await app_settings.set_setting(isbndb.API_KEY_SETTING, body.isbndb_api_key.strip())
    if body.hardcover_api_key is not None:
        await app_settings.set_setting(hardcover.API_KEY_SETTING, body.hardcover_api_key.strip())
    if body.mullvad_account_number is not None:
        digits = _normalize_mullvad_account(body.mullvad_account_number)
        await app_settings.set_setting(MULLVAD_SETTING, digits)
        if digits:
            import asyncio
            from app.services import mullvad as mullvad_svc

            try:
                priv, addr = await asyncio.to_thread(mullvad_svc.register_wireguard, digits)
                await app_settings.set_setting("integrations.mullvad_wg_private_key", priv)
                await app_settings.set_setting("integrations.mullvad_wg_addresses", addr)
                _write_mullvad_env_file(digits, private_key=priv, addresses=addr)
            except Exception as e:
                logger.exception("Mullvad WireGuard registration failed")
                raise HTTPException(
                    status_code=502,
                    detail=f"Mullvad WireGuard registration failed: {e}",
                ) from e
        else:
            await app_settings.set_setting("integrations.mullvad_wg_private_key", "")
            await app_settings.set_setting("integrations.mullvad_wg_addresses", "")
            _write_mullvad_env_file("")
    try:
        from app.services.instance_settings import apply_runtime_overrides, invalidate_cache

        invalidate_cache()
        await apply_runtime_overrides()
    except Exception:
        pass
    return await _integrations_payload()


class ConfigUpdate(BaseModel):
    """Partial map of setting key → value. Empty string clears a DB override."""
    settings: dict[str, str | None]


@router.get("/config")
async def get_instance_config(_admin: User = Depends(require_admin)):
    from app.services import instance_settings as inst

    return await inst.list_config()


@router.put("/config")
async def update_instance_config(
    body: ConfigUpdate,
    _admin: User = Depends(require_admin),
):
    from app.services import instance_settings as inst

    try:
        return await inst.update_config(body.settings or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/setup-status")
async def get_setup_status(_admin: User = Depends(require_admin)):
    from app.services import instance_settings as inst

    return await inst.setup_status()


@router.post("/setup-defaults")
async def post_setup_defaults(_admin: User = Depends(require_admin)):
    """Apply recommended RSS-only scraper defaults (safe for Pi)."""
    from app.services import instance_settings as inst

    await inst.apply_setup_defaults()
    return await inst.setup_status()


@router.get("/ol-catalog")
async def get_ol_catalog_status(_admin: User = Depends(require_admin)):
    """Status of the local Open Library catalog DB / build job."""
    from app.services import ol_catalog_build

    return ol_catalog_build.get_status()


class OlCatalogBuildBody(BaseModel):
    include_editions: bool = False
    skip_download: bool = False


@router.post("/ol-catalog/build")
async def start_ol_catalog_build(
    body: OlCatalogBuildBody | None = None,
    _admin: User = Depends(require_admin),
):
    """Start (or report) a long-running Open Library dump import.

    Warning: multi-GB download and multi-hour build on a Pi. Opt-in only.
    """
    from app.services import ol_catalog_build

    opts = body or OlCatalogBuildBody()
    return await ol_catalog_build.start_build(
        include_editions=bool(opts.include_editions),
        skip_download=bool(opts.skip_download),
    )
