import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, delete, func, or_, select
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

# Finished-stream rule: status=finished OR within 5 minutes of book end
# (total known and total_seconds - progress_seconds <= 300).
FINISHED_NEAR_END_SECONDS = 300


class UserResponse(BaseModel):
    id: int
    username: str
    email: str | None = None
    role: str
    is_active: bool
    allow_audiobook_upload: bool = False
    can_share_books: bool = False
    created_at: str
    last_seen_at: str | None = None
    is_online: bool = False
    requests_total: int = 0
    stream_sessions: int = 0
    finished_streams: int = 0
    last_audiobook_title: str | None = None
    last_audiobook_at: str | None = None
    last_ebook_title: str | None = None
    last_ebook_at: str | None = None

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


def stream_counts_as_finished(
    status: str | None,
    progress_seconds: float | None,
    total_seconds: float | None,
) -> bool:
    """True when a stream is finished or within 5 minutes of the book end.

    Rule: ``status == "finished"`` OR (``total_seconds > 0`` and
    ``total_seconds - progress_seconds <= FINISHED_NEAR_END_SECONDS``).
    ABS play rows have no progress fields, so this applies to stream_history only.
    """
    if (status or "") == "finished":
        return True
    total = float(total_seconds or 0)
    if total <= 0:
        return False
    progress = float(progress_seconds or 0)
    return (total - progress) <= FINISHED_NEAR_END_SECONDS


class SetActiveBody(BaseModel):
    """Partial user update — any field may be omitted."""
    is_active: bool | None = None
    role: str | None = None  # "admin" | "user"
    allow_audiobook_upload: bool | None = None
    can_share_books: bool | None = None


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
    source: str | None = None
    abs_item_id: str | None = None
    created_at: str
    completed_at: str | None
    progress_percent: float | None = None
    progress_bytes: int | None = None
    progress_total_bytes: int | None = None
    progress_speed_bps: float | None = None
    staging_path: str | None = None
    quarantine_reason: str | None = None
    manual_review_url: str | None = None


class SweepReviewCursorBody(BaseModel):
    request_id: int | None = None


class SweepSkipBody(BaseModel):
    request_id: int | None = None


class RejectRequestBody(BaseModel):
    reason: str = "Rejected by admin"
    delete_files: bool = True


class StagingFileDeleteBody(BaseModel):
    """Relative path inside the request staging tree (POSIX, no ..)."""
    path: str


class QuickReviewLoadBody(BaseModel):
    """Optional relative path under staging (book folder). Empty = first target."""
    relative_path: str = ""


class QuickReviewSearchBody(BaseModel):
    query: str = ""
    title: str = ""
    author: str = ""
    series: str = ""
    sequence: str = ""
    narrator: str = ""
    limit: int = 10
    # audible (default) | graphicaudio | soundbooththeater
    provider: str = "audible"


class QuickReviewApplyBody(BaseModel):
    relative_path: str = ""
    selected_result: dict
    edit_mode: str = "full"
    replace_cover: bool = True


class EbookReviewSearchBody(BaseModel):
    query: str = ""
    title: str = ""
    author: str = ""
    limit: int = 12


class EbookReviewApplyBody(BaseModel):
    selected_result: dict
    chapter_id: int | None = None
    target_filename: str | None = None


class ContinueForgeBody(BaseModel):
    """Optional resume point for Continue / Quick Review Continue pipeline."""
    resume_from: str | None = None  # auto|m4b|chapters|folder|finalize|metadata
    m4b_done: bool | None = None
    chapters_done: bool | None = None
    asin: str | None = None


class QuickReviewAsinBody(BaseModel):
    asin: str = ""


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
    users = (
        await db.execute(select(User).order_by(User.created_at.desc()))
    ).scalars().all()

    req_counts = dict(
        (
            await db.execute(
                select(DownloadRequest.user_id, func.count())
                .group_by(DownloadRequest.user_id)
            )
        ).all()
    )

    # Finished = status finished OR within 5 min of book end (total known).
    near_end = and_(
        StreamHistory.total_seconds > 0,
        (StreamHistory.total_seconds - StreamHistory.progress_seconds)
        <= FINISHED_NEAR_END_SECONDS,
    )
    finished_case = case(
        (or_(StreamHistory.status == "finished", near_end), 1),
        else_=0,
    )
    stream_rows = (
        await db.execute(
            select(
                StreamHistory.user_id,
                func.count().label("sessions"),
                func.coalesce(func.sum(finished_case), 0).label("finished"),
            ).group_by(StreamHistory.user_id)
        )
    ).all()
    stream_stats = {
        uid: {"sessions": int(sessions or 0), "finished": int(finished or 0)}
        for uid, sessions, finished in stream_rows
    }

    abs_rows = (
        await db.execute(
            select(
                ABSPlayTracking.user_id,
                func.count().label("titles"),
            ).group_by(ABSPlayTracking.user_id)
        )
    ).all()
    abs_session_counts = {uid: int(titles or 0) for uid, titles in abs_rows}

    # Latest RD stream title/time per user (for Last Book → audiobook).
    sh_max = (
        select(
            StreamHistory.user_id.label("user_id"),
            func.max(StreamHistory.updated_at).label("max_at"),
        )
        .group_by(StreamHistory.user_id)
        .subquery()
    )
    sh_latest_rows = (
        await db.execute(
            select(StreamHistory.user_id, StreamHistory.title, StreamHistory.updated_at)
            .join(
                sh_max,
                and_(
                    StreamHistory.user_id == sh_max.c.user_id,
                    StreamHistory.updated_at == sh_max.c.max_at,
                ),
            )
        )
    ).all()
    last_stream_by_user: dict[int, tuple[str, datetime | None]] = {}
    for uid, title, updated_at in sh_latest_rows:
        if uid not in last_stream_by_user:
            last_stream_by_user[uid] = (title or "", updated_at)

    # Latest ABS play title/time per user.
    abs_max = (
        select(
            ABSPlayTracking.user_id.label("user_id"),
            func.max(ABSPlayTracking.last_played_at).label("max_at"),
        )
        .group_by(ABSPlayTracking.user_id)
        .subquery()
    )
    abs_latest_rows = (
        await db.execute(
            select(
                ABSPlayTracking.user_id,
                ABSPlayTracking.title,
                ABSPlayTracking.last_played_at,
            ).join(
                abs_max,
                and_(
                    ABSPlayTracking.user_id == abs_max.c.user_id,
                    ABSPlayTracking.last_played_at == abs_max.c.max_at,
                ),
            )
        )
    ).all()
    last_abs_by_user: dict[int, tuple[str, datetime | None]] = {}
    for uid, title, last_played in abs_latest_rows:
        if uid not in last_abs_by_user:
            last_abs_by_user[uid] = (title or "", last_played)

    # Ebook reading progress is client-only (localStorage). Best server signal:
    # most recent ebook download request (completed_at, else created_at).
    ebook_at = func.coalesce(DownloadRequest.completed_at, DownloadRequest.created_at)
    ebook_max = (
        select(
            DownloadRequest.user_id.label("user_id"),
            func.max(ebook_at).label("max_at"),
        )
        .where(DownloadRequest.media_type == "ebook")
        .group_by(DownloadRequest.user_id)
        .subquery()
    )
    ebook_latest_rows = (
        await db.execute(
            select(DownloadRequest.user_id, DownloadRequest.title, ebook_at)
            .join(
                ebook_max,
                and_(
                    DownloadRequest.user_id == ebook_max.c.user_id,
                    ebook_at == ebook_max.c.max_at,
                ),
            )
            .where(DownloadRequest.media_type == "ebook")
        )
    ).all()
    last_ebook_by_user: dict[int, tuple[str, datetime | None]] = {}
    for uid, title, at in ebook_latest_rows:
        if uid not in last_ebook_by_user:
            last_ebook_by_user[uid] = (title or "", at)

    now = datetime.now(timezone.utc)
    out: list[UserResponse] = []
    for user in users:
        st = stream_stats.get(user.id, {})
        rd_sessions = int(st.get("sessions") or 0)
        abs_titles = int(abs_session_counts.get(user.id) or 0)

        sh_title, sh_at = last_stream_by_user.get(user.id, ("", None))
        abs_title, abs_at = last_abs_by_user.get(user.id, ("", None))
        audio_at = later_datetime(sh_at, abs_at)
        if audio_at is None:
            last_audio_title = None
        elif abs_at is not None and _as_utc(abs_at) == audio_at:
            last_audio_title = abs_title or None
        else:
            last_audio_title = sh_title or None

        ebook_title, ebook_when = last_ebook_by_user.get(user.id, (None, None))

        # Listening sessions span debrid stream_history AND ABS play tracking.
        out.append(
            UserResponse(
                id=user.id,
                username=user.username,
                email=user.email,
                role=user.role,
                is_active=user.is_active,
                allow_audiobook_upload=bool(getattr(user, "allow_audiobook_upload", False)),
                can_share_books=bool(getattr(user, "can_share_books", False)),
                created_at=_iso(user.created_at) or "",
                last_seen_at=_iso(user.last_seen_at),
                is_online=user_is_online(user.last_seen_at, now=now),
                requests_total=int(req_counts.get(user.id) or 0),
                stream_sessions=rd_sessions + abs_titles,
                finished_streams=int(st.get("finished") or 0),
                last_audiobook_title=last_audio_title,
                last_audiobook_at=_iso(audio_at),
                last_ebook_title=ebook_title or None,
                last_ebook_at=_iso(ebook_when),
            )
        )
    return out


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: int,
    body: SetActiveBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update active flag, role (promote/demote), and/or audiobook upload permission."""
    user = await _get_user_or_404(db, user_id)
    changed: list[str] = []

    if body.is_active is not None:
        if user_id == admin.id:
            raise HTTPException(status_code=400, detail="Cannot change your own active status")
        if body.is_active != user.is_active:
            if not body.is_active:
                await _ensure_not_last_active_admin(db, user, action="disable")
            user.is_active = body.is_active
            changed.append("enabled" if user.is_active else "disabled")

    promoted_to_admin = False
    if body.role is not None:
        new_role = (body.role or "").strip().lower()
        if new_role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="role must be admin or user")
        if user_id == admin.id and new_role != "admin":
            raise HTTPException(status_code=400, detail="Cannot demote yourself")
        if new_role != user.role:
            if user.role == "admin" and new_role == "user":
                await _ensure_not_last_active_admin(db, user, action="demote")
            user.role = new_role
            changed.append(f"role={new_role}")
            if new_role == "admin":
                promoted_to_admin = True

    if body.allow_audiobook_upload is not None:
        user.allow_audiobook_upload = bool(body.allow_audiobook_upload)
        changed.append(
            "upload_on" if user.allow_audiobook_upload else "upload_off"
        )

    if body.can_share_books is not None:
        user.can_share_books = bool(body.can_share_books)
        changed.append(
            "share_on" if user.can_share_books else "share_off"
        )

    if not changed:
        return {
            "message": f"No changes for {user.username}",
            "is_active": user.is_active,
            "role": user.role,
            "allow_audiobook_upload": bool(user.allow_audiobook_upload),
            "can_share_books": bool(user.can_share_books),
        }

    await db.commit()
    await db.refresh(user)

    whitelist_added = False
    whitelist_ip = getattr(user, "last_client_ip", None)
    if promoted_to_admin:
        try:
            from app.services import admin_whitelist

            sync = await admin_whitelist.sync_admin_ips(db)
            whitelist_added = bool(
                whitelist_ip and whitelist_ip in (sync.get("ips") or [])
            )
        except Exception as e:
            logger.warning("LibraForge whitelist sync failed: %s", e)

    return {
        "message": f"Updated {user.username} ({', '.join(changed)})",
        "is_active": user.is_active,
        "role": user.role,
        "allow_audiobook_upload": bool(user.allow_audiobook_upload),
        "can_share_books": bool(user.can_share_books),
        "libraforge_whitelist_added": whitelist_added,
        "libraforge_whitelist_ip": whitelist_ip,
    }


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
            source=getattr(req, "source", None),
            abs_item_id=getattr(req, "abs_item_id", None),
            created_at=req.created_at.isoformat() if req.created_at else "",
            completed_at=req.completed_at.isoformat() if req.completed_at else None,
            progress_percent=req.progress_percent,
            progress_bytes=req.progress_bytes,
            progress_total_bytes=req.progress_total_bytes,
            progress_speed_bps=req.progress_speed_bps,
            staging_path=getattr(req, "staging_path", None),
            quarantine_reason=getattr(req, "quarantine_reason", None),
            # LibraForge Manual Review is audiobook-only; ebooks use Staging files + Continue.
            manual_review_url=(
                review
                if req.status == "quarantined" and (req.media_type or "") != "ebook"
                else None
            ),
        )
        for req, username in result.all()
    ]


@router.post("/download-requests/{request_id}/reject")
async def reject_download_request(
    request_id: int,
    body: RejectRequestBody = Body(default_factory=RejectRequestBody),
    _admin: User = Depends(require_admin),
):
    """Reject a quarantined (or failed) request â€” user sees admin-rejected like a failure."""
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
    body: ContinueForgeBody = Body(default_factory=ContinueForgeBody),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Resume after quarantine: audiobooks → forge steps; ebooks → organize → Kavita.

    Accepts optional ``resume_from`` / done hints so Quick Review can continue
    from M4B, chapters, or Folder Forge depending on what was already done.
    """
    from app.services.forge_pipeline import (
        continue_after_manual_review,
        detect_pipeline_state,
        resolve_resume_from,
        resolve_staging_dir,
    )
    from app.services.ebook_pipeline import continue_ebook_after_review
    from app.services.pipeline import _update_status

    result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    allowed = (
        "quarantined",
        "metadata_forge",
        "m4b_convert",
        "chapter_forge",
        "folder_forge",
    )
    if req.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot continue request in status '{req.status}'",
        )
    if not (req.staging_path or "").strip():
        raise HTTPException(status_code=400, detail="Request has no staging_path")

    is_ebook = (req.media_type or "") == "ebook"

    # Flip out of quarantined before returning so Admin/My Requests refetch
    # immediately sees progress (background task may take a moment to start).
    req.quarantine_reason = None
    await db.commit()

    if is_ebook:
        await _update_status(
            db,
            request_id,
            "folder_forge",
            "Resuming ebook organize after review…",
        )
        asyncio.create_task(continue_ebook_after_review(request_id))
        return {
            "ok": True,
            "id": request_id,
            "status": "folder_forge",
            "message": "Continuing ebook pipeline",
        }

    try:
        staging = resolve_staging_dir(req.staging_path or "")
        step = resolve_resume_from(
            staging,
            resume_from=body.resume_from,
            m4b_done=body.m4b_done,
            chapters_done=body.chapters_done,
        )
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    status_map = {
        "metadata": "metadata_forge",
        "m4b": "m4b_convert",
        "chapters": "chapter_forge",
        "folder": "folder_forge",
        "finalize": "finalizing",
    }
    next_status = status_map.get(step, "m4b_convert")
    await _update_status(
        db,
        request_id,
        next_status,
        f"Resuming after manual review from {step}…",
    )

    asyncio.create_task(
        continue_after_manual_review(
            request_id,
            resume_from=step,
            m4b_done=body.m4b_done,
            chapters_done=body.chapters_done,
            asin_override=body.asin,
        )
    )
    state = detect_pipeline_state(staging)
    return {
        "ok": True,
        "id": request_id,
        "status": next_status,
        "resume_from": step,
        "pipeline": state,
        "message": f"Continuing LibraForge pipeline from {step}",
    }


@router.post("/download-requests/{request_id}/rerun-pipeline")
async def rerun_pipeline(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Re-stage a finished audiobook into Quick Review (metadata → M4B → chapters → folder).

    Copies from the library folder when staging is gone. Quarantines via
    ``_set_quarantine`` so admin-review notifications still fire.
    """
    from app.services.forge_pipeline import prepare_pipeline_rerun

    # Touch db session so dependency stays consistent with other admin routes.
    _ = db
    try:
        return await prepare_pipeline_rerun(request_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
    suggest_prune: bool = False,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List the request's ``.unorganized`` staging tree (admin file browser)."""
    from app.services import llm_assist
    from app.services.forge_pipeline import build_staging_tree, resolve_staging_dir

    req = await _staging_request_or_404(db, request_id)
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if suggest_prune:
        try:
            # Reuse cached plan when present — avoid a new LLM call every Files open.
            await llm_assist.maybe_auto_prune_or_suggest(
                request_id,
                staging=staging,
                force_suggest=not bool(llm_assist.read_assist(staging).get("file_prune")),
            )
        except Exception:
            logger.debug("Prune suggest on staging-files soft-fail", exc_info=True)
    tree = build_staging_tree(staging)
    assist = llm_assist.read_assist(staging)
    return {
        "request_id": request_id,
        "title": req.title,
        "status": req.status,
        "llm_assist": assist or None,
        **tree,
    }


class LlmPruneApplyBody(BaseModel):
    paths: list[str] | None = None  # None = apply plan deletes; else selected paths


@router.post("/requests/{request_id}/llm-assist/apply-prune")
@router.post("/download-requests/{request_id}/llm-assist/apply-prune")
async def apply_llm_file_prune(
    request_id: int,
    body: LlmPruneApplyBody | None = None,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Apply OpenRouter file-prune suggestions (staging-only, path-safe)."""
    from app.services import llm_assist
    from app.services.forge_pipeline import resolve_staging_dir

    req = await _staging_request_or_404(db, request_id)
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    paths = body.paths if body else None
    deleted = llm_assist.apply_file_prune(staging, paths=paths, only_safe_duplicates=False)
    return {
        "ok": True,
        "deleted": deleted,
        "llm_assist": llm_assist.read_assist(staging),
    }


@router.post("/requests/{request_id}/llm-assist/apply-split")
@router.post("/download-requests/{request_id}/llm-assist/apply-split")
async def apply_llm_multi_book_split(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Apply stored multi-book split plan → child requests + forge jobs."""
    from app.services import llm_assist
    from app.services.forge_pipeline import resolve_staging_dir

    req = await _staging_request_or_404(db, request_id)
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    plan_raw = llm_assist.read_assist(staging).get("multi_book_split")
    if not plan_raw:
        raise HTTPException(status_code=400, detail="No multi-book split plan on this request")
    try:
        child_ids = await llm_assist.apply_multi_book_split(
            request_id, staging=staging, spawn_forge=True
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Apply split failed for request %s", request_id)
        raise HTTPException(status_code=500, detail=str(e)[:300]) from e
    return {"ok": True, "child_ids": child_ids}


@router.get("/requests/{request_id}/llm-assist")
@router.get("/download-requests/{request_id}/llm-assist")
async def get_llm_assist(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return staging ``llm_assist.json`` suggestions for Quick Review."""
    from app.services import llm_assist
    from app.services.forge_pipeline import resolve_staging_dir

    req = await _staging_request_or_404(db, request_id)
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"request_id": request_id, "llm_assist": llm_assist.read_assist(staging) or None}


@router.delete("/requests/{request_id}/staging-files")
@router.delete("/download-requests/{request_id}/staging-files")
async def delete_request_staging_file(
    request_id: int,
    body: StagingFileDeleteBody,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete one file or folder (recursive) under the request staging tree.

    Nested directories may contain files; the staging root cannot be deleted.
    """
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



@router.get("/requests/{request_id}/quick-review")
@router.get("/download-requests/{request_id}/quick-review")
async def get_quick_review(
    request_id: int,
    relative_path: str = "",
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Load Quick Admin Review clues (Files → Metadata wizard)."""
    from app.services.quick_review import QuickReviewError, load_quick_review

    req = await _staging_request_or_404(db, request_id)
    try:
        return await load_quick_review(req, relative_path=relative_path or None)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except QuickReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/requests/{request_id}/quick-review/load")
@router.post("/download-requests/{request_id}/quick-review/load")
async def post_quick_review_load(
    request_id: int,
    body: QuickReviewLoadBody = Body(default_factory=QuickReviewLoadBody),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reload clues for a chosen staging target path."""
    from app.services.quick_review import QuickReviewError, load_quick_review

    req = await _staging_request_or_404(db, request_id)
    try:
        return await load_quick_review(req, relative_path=body.relative_path or None)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except QuickReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/requests/{request_id}/quick-review/search")
@router.post("/download-requests/{request_id}/quick-review/search")
async def post_quick_review_search(
    request_id: int,
    body: QuickReviewSearchBody,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Search Audible metadata candidates via LibraForge (admin proxy)."""
    from app.services.quick_review import QuickReviewError, search_quick_review

    req = await _staging_request_or_404(db, request_id)
    try:
        return await search_quick_review(
            req,
            query=body.query,
            title=body.title,
            author=body.author,
            series=body.series,
            sequence=body.sequence,
            narrator=body.narrator,
            limit=body.limit,
            provider=body.provider,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except QuickReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/requests/{request_id}/quick-review/apply")
@router.post("/download-requests/{request_id}/quick-review/apply")
async def post_quick_review_apply(
    request_id: int,
    body: QuickReviewApplyBody,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Apply selected metadata to staging (overwrite tags + replace cover)."""
    from app.services.quick_review import QuickReviewError, apply_quick_review

    req = await _staging_request_or_404(db, request_id)
    try:
        result = await apply_quick_review(
            req,
            relative_path=body.relative_path or None,
            selected_result=body.selected_result,
            edit_mode=body.edit_mode,
            replace_cover=body.replace_cover,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except QuickReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "Admin %s applied Quick Review metadata for request %s (mode=%s)",
        _admin.username,
        request_id,
        result.get("edit_mode"),
    )
    return result


@router.get("/requests/{request_id}/ebook-review")
@router.get("/download-requests/{request_id}/ebook-review")
async def get_ebook_review(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Load ebook Quick Review clues (Files → Hardcover metadata wizard)."""
    from app.services.ebook_quick_review import (
        EbookQuickReviewError,
        load_ebook_quick_review,
    )

    req = await _staging_request_or_404(db, request_id)
    try:
        return await load_ebook_quick_review(req)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except EbookQuickReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/requests/{request_id}/ebook-review/search")
@router.post("/download-requests/{request_id}/ebook-review/search")
async def post_ebook_review_search(
    request_id: int,
    body: EbookReviewSearchBody,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Search Hardcover + Open Library metadata candidates for a quarantined ebook."""
    from app.services.ebook_quick_review import (
        EbookQuickReviewError,
        search_ebook_quick_review,
    )

    req = await _staging_request_or_404(db, request_id)
    try:
        return await search_ebook_quick_review(
            req,
            query=body.query,
            title=body.title,
            author=body.author,
            limit=body.limit,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except EbookQuickReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/requests/{request_id}/ebook-review/apply")
@router.post("/download-requests/{request_id}/ebook-review/apply")
async def post_ebook_review_apply(
    request_id: int,
    body: EbookReviewApplyBody,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Apply selected Hardcover/Open Library metadata to an ebook staging request."""
    from app.services.ebook_quick_review import (
        EbookQuickReviewError,
        apply_ebook_quick_review,
    )

    req = await _staging_request_or_404(db, request_id)
    try:
        result = await apply_ebook_quick_review(
            req,
            selected_result=body.selected_result,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except EbookQuickReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "Admin %s applied ebook metadata for request %s",
        _admin.username,
        request_id,
    )
    return result


@router.get("/requests/{request_id}/quick-review/pipeline-state")
@router.get("/download-requests/{request_id}/quick-review/pipeline-state")
async def get_quick_review_pipeline_state(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Detect M4B / ASIN / metadata readiness for Quick Review steps."""
    from app.services.forge_pipeline import detect_pipeline_state, resolve_staging_dir

    req = await _staging_request_or_404(db, request_id)
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {
        "request_id": request_id,
        "status": req.status,
        "status_detail": req.status_detail,
        **detect_pipeline_state(staging),
    }


@router.post("/requests/{request_id}/quick-review/m4b")
@router.post("/download-requests/{request_id}/quick-review/m4b")
async def post_quick_review_m4b(
    request_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Run M4B convert only for this staging request (then return to Quick Review)."""
    from app.services.forge_pipeline import (
        detect_pipeline_state,
        resolve_staging_dir,
        run_forge_after_download,
    )
    from app.services.pipeline import _update_status

    req = await _staging_request_or_404(db, request_id)
    if (req.media_type or "") == "ebook":
        raise HTTPException(status_code=400, detail="M4B is audiobook-only")
    try:
        staging = resolve_staging_dir(req.staging_path or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    state = detect_pipeline_state(staging)
    if not state["needs_m4b"]:
        return {
            "ok": True,
            "skipped": True,
            "message": "Already a single M4B — nothing to convert",
            "pipeline": state,
        }

    req.quarantine_reason = None
    await db.commit()
    await _update_status(
        db, request_id, "m4b_convert", "Queued for M4B (Quick Review)…"
    )
    user_id = req.user_id
    title = req.title
    author = req.author

    async def _run() -> None:
        await run_forge_after_download(
            request_id,
            staging=staging,
            user_id=user_id,
            title=title,
            author=author,
            resume_from="m4b",
            stop_after="m4b",
        )

    asyncio.create_task(_run())
    return {
        "ok": True,
        "id": request_id,
        "status": "m4b_convert",
        "message": "M4B enqueued — only one convert runs at a time; progress on the request card",
        "pipeline": state,
        "m4b_url": state.get("m4b_url"),
    }


@router.post("/requests/{request_id}/quick-review/chapters/preview")
@router.post("/download-requests/{request_id}/quick-review/chapters/preview")
async def post_quick_review_chapters_preview(
    request_id: int,
    body: QuickReviewAsinBody = Body(default_factory=QuickReviewAsinBody),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fetch Audible chapters for ASIN (no embed) for visual confirm."""
    from app.services import libraforge as lf
    from app.services.forge_pipeline import preview_audible_chapters

    await _staging_request_or_404(db, request_id)
    try:
        return await preview_audible_chapters(request_id, asin=body.asin or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except lf.LibraForgeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/requests/{request_id}/quick-review/chapters/apply")
@router.post("/download-requests/{request_id}/quick-review/chapters/apply")
async def post_quick_review_chapters_apply(
    request_id: int,
    body: QuickReviewAsinBody = Body(default_factory=QuickReviewAsinBody),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Embed Audible chapters into staging .m4b after admin confirm."""
    from app.services import libraforge as lf
    from app.services.forge_pipeline import apply_audible_chapters

    await _staging_request_or_404(db, request_id)
    try:
        return await apply_audible_chapters(request_id, asin=body.asin or "")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except lf.LibraForgeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


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

    author, book_title = downloader.parse_torrent_name(
        req.title, indexer=req.indexer
    )
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
            r"\s*(?:Books?|Vol(?:ume)?s?)\s*1\s*[-â€“]\s*\d+\s*$", "", book_title, flags=re.IGNORECASE
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


# --- Library refresh (ABS + Kavita) ---

@router.post("/library/refresh")
async def library_refresh(_admin: User = Depends(require_admin)):
    """Kick the serialized ABS → Kavita refresh pipeline (never parallel scans).

    Returns immediately; poll ``GET /admin/library/refresh/status`` for
    completion. Parallel full scans on the Pi caused OOM freezes, so all
    refresh entry points coalesce onto one background pipeline.
    """
    from app.services import library_refresh as refresh_pipeline

    kick = refresh_pipeline.kick()
    deferred = bool(kick.get("started"))
    return {
        "ok": bool(kick.get("ok")),
        "message": kick.get("message") or "Library refresh",
        "abs": {
            "ok": bool(kick.get("ok")),
            "scan_ran": deferred,
            "scan_complete": False,
            "timed_out": False,
            "deferred": deferred,
            "already_running": bool(kick.get("already_running")),
            "items_total": None,
            "error": None,
            "message": kick.get("message"),
        },
        "kavita": {
            "ok": bool(kick.get("ok")),
            "error": None,
            "message": "Kavita scan queued after ABS completes",
        },
        "started": deferred,
        "already_running": bool(kick.get("already_running")),
        "cooldown": bool(kick.get("cooldown")),
    }


@router.get("/library/refresh/status")
async def library_refresh_status(_admin: User = Depends(require_admin)):
    """Current phase of the refresh pipeline (idle | abs | kavita)."""
    from app.services import library_refresh as refresh_pipeline

    return refresh_pipeline.get_status()


@router.post("/abs/fix-metadata")
async def fix_abs_metadata(_admin: User = Depends(require_admin)):
    """Scan ABS and purge missing/orphan items. Does not rewrite titles or Quick Match.

    Prefer ``POST /admin/library/refresh`` (ABS+Kavita, deferred). This endpoint
    now also uses a deferred ABS kick to avoid Pi OOM from blocking full scans.
    """
    result = await audiobookshelf.fix_metadata_mismatches()
    if result.get("fetch_error"):
        raise HTTPException(status_code=502, detail=result["fetch_error"])
    return result


@router.post("/abs/rematch/{item_id}")
async def rematch_abs_item(
    item_id: str,
    _admin: User = Depends(require_admin),
):
    """Fill missing ABS fields via Audible Quick Match (does not force-overwrite).

    Skips books that already have an ASIN so LibraForge / manual metadata is not
    replaced by another provider pass.
    """
    result = await audiobookshelf.match_item(item_id, override_defaults=False)
    if result is None:
        raise HTTPException(status_code=502, detail="Failed to match item in ABS")
    if result.get("skipped"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Skipped Quick Match — ASIN already set ({result.get('asin')}). "
                "LibraForge / manual metadata is protected; rematch only fills gaps "
                "on books without an ASIN."
            ),
        )
    return {"updated": result.get("updated", False), "skipped": False}


@router.get("/library/abs/{item_id}/metadata-review")
async def get_abs_library_metadata_review(
    item_id: str,
    _admin: User = Depends(require_admin),
):
    """Load metadata match clues for an in-library audiobook."""
    from app.services.library_metadata_review import (
        LibraryMetadataReviewError,
        load_abs_metadata_review,
    )

    try:
        return await load_abs_metadata_review(item_id)
    except LibraryMetadataReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/library/abs/{item_id}/metadata-review/search")
async def post_abs_library_metadata_search(
    item_id: str,
    body: QuickReviewSearchBody,
    _admin: User = Depends(require_admin),
):
    """Search Audible / specialty catalogs for a library audiobook."""
    from app.services.library_metadata_review import (
        LibraryMetadataReviewError,
        search_abs_metadata_review,
    )

    try:
        return await search_abs_metadata_review(
            item_id,
            query=body.query,
            title=body.title,
            author=body.author,
            series=body.series,
            sequence=body.sequence,
            narrator=body.narrator,
            limit=body.limit,
            provider=body.provider,
        )
    except LibraryMetadataReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/library/abs/{item_id}/metadata-review/apply")
async def post_abs_library_metadata_apply(
    item_id: str,
    body: QuickReviewApplyBody,
    _admin: User = Depends(require_admin),
):
    """Apply selected metadata to a library audiobook (files + ABS)."""
    from app.services.library_metadata_review import (
        LibraryMetadataReviewError,
        apply_abs_metadata_review,
    )

    try:
        result = await apply_abs_metadata_review(
            item_id,
            selected_result=body.selected_result,
            edit_mode=body.edit_mode,
            replace_cover=body.replace_cover,
        )
    except LibraryMetadataReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "Admin %s applied library audiobook metadata for item %s",
        _admin.username,
        item_id,
    )
    return result


@router.get("/library/ebook/{series_id}/metadata-review")
async def get_ebook_library_metadata_review(
    series_id: int,
    chapter_id: int | None = Query(None),
    target_filename: str | None = Query(None),
    _admin: User = Depends(require_admin),
):
    """Load metadata match clues for an in-library ebook (optionally one volume)."""
    from app.services.library_metadata_review import (
        LibraryMetadataReviewError,
        load_ebook_metadata_review,
    )

    try:
        return await load_ebook_metadata_review(
            series_id,
            chapter_id=chapter_id,
            target_filename=target_filename,
        )
    except LibraryMetadataReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/library/ebook/{series_id}/metadata-review/search")
async def post_ebook_library_metadata_search(
    series_id: int,
    body: EbookReviewSearchBody,
    _admin: User = Depends(require_admin),
):
    """Search Hardcover + Open Library for a library ebook."""
    from app.services.library_metadata_review import (
        LibraryMetadataReviewError,
        search_ebook_metadata_review,
    )

    try:
        return await search_ebook_metadata_review(
            series_id,
            query=body.query,
            title=body.title,
            author=body.author,
            limit=body.limit,
        )
    except LibraryMetadataReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/library/ebook/{series_id}/metadata-review/apply")
async def post_ebook_library_metadata_apply(
    series_id: int,
    body: EbookReviewApplyBody,
    _admin: User = Depends(require_admin),
):
    """Apply selected metadata to a library ebook (OPF embed + Kavita)."""
    from app.services.library_metadata_review import (
        LibraryMetadataReviewError,
        apply_ebook_metadata_review,
    )

    try:
        result = await apply_ebook_metadata_review(
            series_id,
            selected_result=body.selected_result,
            chapter_id=body.chapter_id,
            target_filename=body.target_filename,
        )
    except LibraryMetadataReviewError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "Admin %s applied library ebook metadata for series %s chapter=%s file=%s",
        _admin.username,
        series_id,
        body.chapter_id,
        body.target_filename,
    )
    return result


@router.delete("/library/abs/{item_id}")
async def delete_abs_library_media(
    item_id: str,
    _admin: User = Depends(require_admin),
):
    """Delete on-disk audiobook folder and soft-remove the ABS library item."""
    from app.config import get_settings
    from app.services.library_media_delete import (
        delete_tree_under_library,
        get_abs_forbidden_dirnames,
        resolve_abs_book_dir,
    )

    settings = get_settings()
    item = await audiobookshelf.get_library_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    audiobook_dir = Path(settings.audiobook_dir)
    try:
        book_dir = resolve_abs_book_dir(audiobook_dir, item)
        delete_tree_under_library(book_dir, audiobook_dir, get_abs_forbidden_dirnames())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    deleted = await audiobookshelf.delete_library_item(item_id, hard=False)
    if not deleted:
        raise HTTPException(status_code=502, detail="Failed to remove item from Audiobookshelf")
    return {"deleted": True, "itemId": item_id}


@router.delete("/library/ebook/{series_id}")
async def delete_ebook_library_media(
    series_id: int,
    _admin: User = Depends(require_admin),
):
    """Delete on-disk ebook files/dirs and remove the Kavita series."""
    from app.config import get_settings
    from app.services.library_media_delete import (
        delete_tree_under_library,
        get_ebook_forbidden_dirnames,
        resolve_ebook_book_dirs,
    )

    settings = get_settings()
    file_paths = await kavita.get_series_local_file_paths(series_id)
    ebook_dir = Path(settings.ebook_dir)
    try:
        for book_dir in resolve_ebook_book_dirs(ebook_dir, file_paths):
            delete_tree_under_library(book_dir, ebook_dir, get_ebook_forbidden_dirnames())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    deleted = await kavita.delete_series(series_id)
    if not deleted:
        try:
            await kavita.scan_library()
        except Exception:
            logger.exception("Kavita scan after failed series delete")
        raise HTTPException(status_code=502, detail="Failed to remove series from Kavita")
    try:
        await kavita.scan_library()
    except Exception:
        logger.debug("Kavita scan after ebook delete failed", exc_info=True)
    return {"deleted": True, "seriesId": series_id}


# --- System Health ---

@router.get("/health")
async def system_health(_admin: User = Depends(require_admin)):
    from app.services.health_checks import collect_system_health

    return await collect_system_health()


@router.get("/health/pending-actions")
async def health_pending_actions(_admin: User = Depends(require_admin)):
    """Aggregated admin review queues for the Health dashboard (counts + deep links)."""
    from app.services.pending_actions import collect_pending_actions

    return await collect_pending_actions()


@router.get("/docker/services")
async def docker_services(_admin: User = Depends(require_admin)):
    """List Docker-managed stack services and live container state (admin only)."""
    from app.services import docker_control

    return await docker_control.list_services()


@router.post("/docker/services/{service_id}/{action}")
async def docker_service_action(
    service_id: str,
    action: str,
    _admin: User = Depends(require_admin),
):
    """Start / stop / restart a managed Docker service (admin only).

    The Library app container (`app`) only allows restart, scheduled after the
    response so the HTTP request can complete before the process exits.
    """
    from app.services import docker_control

    action_norm = (action or "").strip().lower()
    if action_norm not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="action must be start, stop, or restart")

    if docker_control.get_service(service_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_id}")

    try:
        return await docker_control.control_service(service_id, action_norm)  # type: ignore[arg-type]
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("Docker %s on %s failed", action_norm, service_id)
        raise HTTPException(status_code=502, detail=str(e)[:240]) from e



# --- Server stack update (host git + compose via docker.sock bridge) ---

@router.get("/server-update/status")
async def server_update_status(_admin: User = Depends(require_admin)):
    """Current installed revision + whether host apply is available."""
    from app.services import server_update

    return await server_update.get_status()


@router.post("/server-update/check")
async def server_update_check(_admin: User = Depends(require_admin)):
    """Compare local install SHA to origin/main (GitHub) without applying."""
    from app.services import server_update

    return await server_update.check_for_updates()


@router.post("/server-update/apply")
async def server_update_apply(_admin: User = Depends(require_admin)):
    """Start async full-stack update (same as scripts/update_library.sh --force)."""
    from app.services import server_update

    try:
        return await server_update.start_apply()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/server-update/job")
async def server_update_job(_admin: User = Depends(require_admin)):
    """Poll apply job phase + log tail (persisted under data/)."""
    from app.services import server_update

    return await server_update.get_job()


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


@router.get("/kavita/opds-status")
async def kavita_opds_status(_admin: User = Depends(require_admin)):
    """Probe Kavita's native OPDS feed using the configured API key."""
    from app.services import opds as opds_svc

    return await opds_svc.probe_kavita_opds()


@router.get("/kavita-debug")
async def kavita_debug(_admin: User = Depends(require_admin)):
    """Diagnostic endpoint for Kavita ebook loading.

    ``ebook_count`` is the shelf card total (volume/file expansion) — same unit as
    My Library after refresh. ``ebook_series_count`` is EPUB/PDF series rows.
    """
    from app.config import get_settings
    from app.routers.library import kavita_ebook_inventory
    import httpx

    settings = get_settings()
    result = {
        "kavita_url": settings.kavita_url,
        "api_key_set": bool(settings.kavita_api_key),
        "library_id": settings.kavita_library_id,
        "health_ok": False,
        "series_api_ok": False,
        "series_count": 0,
        "ebook_series_count": 0,
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

    # Inventory via the same series + volume expansion as the library shelf.
    try:
        inv = await kavita_ebook_inventory(force_refresh=True)
        result["series_api_ok"] = True
        result["series_count"] = inv["series_count"]
        result["ebook_series_count"] = inv["ebook_series_count"]
        result["ebook_count"] = inv["ebook_count"]
        # Small raw sample for debugging (ebook-format series only).
        ebook_series = await kavita.get_all_series(
            formats=kavita.EBOOK_FORMATS, force_refresh=False
        )
        result["raw_sample"] = ebook_series[:2] if ebook_series else None
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
    openrouter_enabled: bool | None = None
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None
    openrouter_confidence_threshold: float | None = None
    mullvad_account_number: str | None = None


def _mask(secret: str) -> str:
    """Show only the last 4 chars so admins can confirm which key is stored."""
    if not secret:
        return ""
    return ("*" * max(0, len(secret) - 4)) + secret[-4:]


MULLVAD_SETTING = "integrations.mullvad_account_number"
_MULLVAD_ENV_PATH = Path("/app/data/mullvad.env")


def _normalize_mullvad_account(raw: str) -> str:
    """Strip spaces/dashes â€” Mullvad account numbers are 16 digits."""
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
            # Account alone is not enough for WireGuard â€” keys required.
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
    from app.services import nyt_books, isbndb, hardcover, openrouter, app_settings
    from app.services import instance_settings as inst

    stored = await app_settings.get_setting(nyt_books.API_KEY_SETTING, default="")
    effective = await nyt_books.get_api_key()
    isbn_stored = await app_settings.get_setting(isbndb.API_KEY_SETTING, default="")
    isbn_effective = await isbndb.get_api_key()
    hc_stored = await app_settings.get_setting(hardcover.API_KEY_SETTING, default="")
    hc_effective = await hardcover.get_api_key()
    or_stored = await app_settings.get_setting(openrouter.API_KEY_SETTING, default="")
    or_effective = await openrouter.get_api_key()
    or_enabled = await inst.get_effective_bool(openrouter.ENABLED_SETTING, False)
    or_model = await openrouter.get_model()
    or_threshold = await openrouter.get_confidence_threshold()
    or_usage = None
    if or_effective:
        try:
            or_usage = (await openrouter.fetch_key_usage()).to_dict()
        except Exception:
            or_usage = {"error": "Could not load usage"}
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
        "openrouter": {
            "enabled": or_enabled,
            "configured": bool(or_effective),
            "overridden": bool(or_stored),
            "hint": _mask(or_effective),
            "model": or_model,
            "confidenceThreshold": or_threshold,
            "usage": or_usage,
            "note": "OpenRouter LLM assist (off by default): Metadata Forge / ebook "
                    "identify retry, multi-book split, file prune suggestions, and "
                    "ASIN recovery. Requires an API key. Usage below is from "
                    "OpenRouter GET /api/v1/key (per-key credits).",
        },
        "mullvad": {
            "configured": bool(mullvad_eff),
            "overridden": bool(mullvad_stored),
            "hint": _mask(mullvad_eff),
            "wireguardReady": bool(wg_key and wg_addr),
            "wireguardHint": _mask(wg_addr) if wg_addr else "",
            "note": "Only ABB traffic uses Mullvad (FlareSolverr â†’ gluetun:8888). "
                    "Jackett/Knaben/Prowlarr stay on your LAN. Saving an account "
                    "auto-registers WireGuard keys into data/mullvad.env â€” then "
                    "restart: docker compose up -d gluetun",
        },
        "audible": await _audible_integrations_slice(),
    }


async def _audible_integrations_slice() -> dict:
    """Audible auth status for Integrations panel (proxied; never returns auth JSON)."""
    from app.services import libraforge as lf

    try:
        return await lf.audible_auth_summary()
    except Exception as e:
        return {
            "configured": False,
            "reachable": False,
            "auth_ok": False,
            "active_name": "",
            "activation_bytes_set": False,
            "accounts": [],
            "locales": {},
            "auth_file": "/auth/audible-metadata.json",
            "libraforge_accounts_url": lf.public_accounts_url(),
            "error": str(e),
            "status": "unreachable",
            "note": (
                "Metadata Forge / Chapter Forge need an Audible auth file on LibraForge "
                "(/auth/audible-metadata.json)."
            ),
        }


@router.get("/integrations")
async def get_integrations(_admin: User = Depends(require_admin)):
    return await _integrations_payload()


@router.get("/integrations/openrouter-usage")
async def get_openrouter_usage(_admin: User = Depends(require_admin)):
    """Refresh OpenRouter per-key usage (GET /api/v1/key). Never returns the raw key."""
    from app.services import openrouter

    if not await openrouter.get_api_key():
        return {"usage": {"error": "No API key configured"}}
    usage = await openrouter.fetch_key_usage()
    return {"usage": usage.to_dict()}


@router.put("/integrations")
async def update_integrations(
    body: IntegrationKeysUpdate,
    _admin: User = Depends(require_admin),
):
    from app.services import nyt_books, isbndb, hardcover, openrouter, app_settings

    if body.nyt_api_key is not None:
        await app_settings.set_setting(nyt_books.API_KEY_SETTING, body.nyt_api_key.strip())
    if body.isbndb_api_key is not None:
        await app_settings.set_setting(isbndb.API_KEY_SETTING, body.isbndb_api_key.strip())
    if body.hardcover_api_key is not None:
        await app_settings.set_setting(hardcover.API_KEY_SETTING, body.hardcover_api_key.strip())
    if body.openrouter_enabled is not None:
        await app_settings.set_setting(
            openrouter.ENABLED_SETTING,
            "true" if body.openrouter_enabled else "false",
        )
    if body.openrouter_api_key is not None:
        await app_settings.set_setting(
            openrouter.API_KEY_SETTING, body.openrouter_api_key.strip()
        )
    if body.openrouter_model is not None:
        model = body.openrouter_model.strip() or openrouter.DEFAULT_MODEL
        await app_settings.set_setting(openrouter.MODEL_SETTING, model)
    if body.openrouter_confidence_threshold is not None:
        threshold = max(0.0, min(1.0, float(body.openrouter_confidence_threshold)))
        await app_settings.set_setting(
            openrouter.CONFIDENCE_SETTING, f"{threshold:.2f}"
        )
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
    """Partial map of setting key â†’ value. Empty string clears a DB override."""
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


class AudibleLoginStartBody(BaseModel):
    locale: str = "us"
    flavor_name: str = "Metadata"


class AudibleLoginCompleteBody(BaseModel):
    redirect_url: str
    login_session_id: str = ""


class AudibleDisconnectBody(BaseModel):
    force: bool = False


def _humanize_audible_complete_error(detail: str) -> str:
    """Prefer LibraForge's humanized register errors; map raw Amazon InvalidValue if needed."""
    text = (detail or "").strip()
    lower = text.lower()
    if "invalidvalue" in lower or "one or more provided values are invalid" in lower:
        if "amazon rejected device registration" in lower or "click sign in once" in lower:
            return text
        return (
            "Amazon rejected device registration (InvalidValue). Usually the authorization "
            "code does not match this Sign-in session — e.g. you clicked Sign in again after "
            "opening Amazon, reused an old dog-page URL, or the code expired. Click Sign in "
            "once, finish Amazon in that tab, then paste the dog-page address bar immediately "
            "(do not retry Complete with the same URL)."
        )
    if text.startswith("Audible registration failed:"):
        return text
    return text


def _lf_http_error(exc: Exception) -> HTTPException:
    from app.services.libraforge import LibraForgeError

    msg = str(exc)
    if isinstance(exc, LibraForgeError):
        if "unreachable" in msg.lower():
            return HTTPException(status_code=502, detail=msg)
        if "HTTP 400" in msg:
            detail = _humanize_audible_complete_error(msg.split(": ", 1)[-1])
            return HTTPException(status_code=400, detail=detail)
        if "HTTP 404" in msg:
            return HTTPException(status_code=404, detail=msg.split(": ", 1)[-1])
        return HTTPException(status_code=502, detail=msg)
    return HTTPException(status_code=500, detail=msg)


@router.get("/audible-auth")
async def get_audible_auth(_admin: User = Depends(require_admin)):
    """Audible credential status via LibraForge (auth JSON never leaves LibraForge)."""
    from app.services import libraforge as lf

    return await lf.audible_auth_summary()


def _looks_like_audible_oauth_url(url: str) -> bool:
    """Amazon 404s if the PKCE query string is truncated — require a full /ap/signin URL."""
    u = (url or "").strip()
    if not u.startswith("https://www.amazon.") and not u.startswith("https://www.audible."):
        return False
    if "/ap/signin?" not in u:
        return False
    return u.count("&") >= 5 and "openid.oa2.code_challenge=" in u


def _redirect_has_audible_auth_code(url: str) -> bool:
    u = (url or "").strip()
    return "openid.oa2.authorization_code=" in u


def _looks_like_audible_oauth_start_url(url: str) -> bool:
    """True when paste is the login/start URL, not the post-login redirect."""
    u = (url or "").strip()
    if not u or _redirect_has_audible_auth_code(u):
        return False
    if "/ap/signin" in u:
        return True
    if "openid.oa2.code_challenge=" in u:
        return True
    return (
        "openid.oa2.response_type=code" in u
        and "openid.return_to=" in u
        and "openid.oa2.authorization_code=" not in u
    )


def _diagnose_audible_redirect_url(url: str) -> str | None:
    """Return an error detail if the paste cannot complete login, else None."""
    u = (url or "").strip()
    if not u:
        return (
            "Paste the address-bar URL from the Amazon dog / Page Not Found page "
            "after you finish signing in."
        )
    if _redirect_has_audible_auth_code(u):
        return None
    if _looks_like_audible_oauth_start_url(u):
        return (
            "That's the Amazon login page URL (it has code_challenge /ap/signin). "
            "Open it, finish signing in, then copy the address bar from the dog/Page Not Found "
            "page — it must include openid.oa2.authorization_code."
        )
    if "/ap/ext/oauth/2" in u:
        return (
            "That looks like Amazon's OAuth endpoint path, not the post-login redirect. "
            "On the dog/Page Not Found page, Select all in the address bar (Ctrl+A) and copy — "
            "the URL should be …/ap/maplanding?…&openid.oa2.authorization_code=…"
        )
    if "/ap/maplanding" in u:
        return (
            "That maplanding URL has no openid.oa2.authorization_code. "
            "Copy the entire address bar (Ctrl+L, Ctrl+A, Ctrl+C). "
            "If there is still no authorization_code, retry Sign in in a private/incognito window."
        )
    return (
        "Redirect URL is missing openid.oa2.authorization_code. "
        "After Amazon sign-in you should land on a Page Not Found / dog page — that is expected. "
        "Copy the entire address bar (usually …/ap/maplanding?…), not the login link and not "
        "just the path without query parameters."
    )


@router.post("/audible-auth/login/start")
async def post_audible_login_start(
    body: AudibleLoginStartBody,
    _admin: User = Depends(require_admin),
):
    """Start Audible OAuth through LibraForge — returns ``oauth_url`` to open in a browser."""
    from app.services import libraforge as lf
    from app.services.libraforge import LibraForgeError

    name = (body.flavor_name or "").strip() or "Metadata"
    try:
        data = await lf.auth_login_start(locale=(body.locale or "us").strip(), flavor_name=name)
    except LibraForgeError as e:
        raise _lf_http_error(e) from e
    oauth_url = str(data.get("oauth_url") or "").strip()
    if not oauth_url:
        raise HTTPException(
            status_code=502,
            detail="LibraForge returned no OAuth URL. Check LibraForge health / version.",
        )
    if not _looks_like_audible_oauth_url(oauth_url):
        raise HTTPException(
            status_code=502,
            detail=(
                "LibraForge returned a malformed Amazon login URL (missing OAuth query params). "
                "Amazon will show a 404 dog page if that truncated URL is opened. "
                "Retry, or use LibraForge Settings → Accounts."
            ),
        )
    return {
        "oauth_url": oauth_url,
        "login_session_id": str(data.get("login_session_id") or ""),
        "locale": (body.locale or "us").strip(),
        "flavor_name": name,
    }


@router.post("/audible-auth/login/complete")
async def post_audible_login_complete(
    body: AudibleLoginCompleteBody,
    _admin: User = Depends(require_admin),
):
    """Finish Audible OAuth with the Amazon redirect URL; LibraForge writes the auth file."""
    from app.services import libraforge as lf
    from app.services.libraforge import LibraForgeError

    redirect = (body.redirect_url or "").strip()
    diagnose = _diagnose_audible_redirect_url(redirect)
    if diagnose:
        raise HTTPException(status_code=400, detail=diagnose)
    try:
        result = await lf.auth_login_complete(
            redirect_url=redirect,
            login_session_id=(body.login_session_id or "").strip(),
        )
    except LibraForgeError as e:
        raise _lf_http_error(e) from e
    summary = await lf.audible_auth_summary()
    return {"ok": True, "login": result, **summary}


@router.post("/audible-auth/disconnect")
async def post_audible_disconnect(
    body: AudibleDisconnectBody,
    _admin: User = Depends(require_admin),
):
    """Disconnect the active Audible account on LibraForge (optional force skips deregister)."""
    from app.services import libraforge as lf
    from app.services.libraforge import LibraForgeError

    try:
        await lf.auth_disconnect(force=bool(body.force))
    except LibraForgeError as e:
        raise _lf_http_error(e) from e
    return await lf.audible_auth_summary()


@router.post("/setup-validate")
async def post_setup_validate(_admin: User = Depends(require_admin)):
    """Soft-probe ABS / Kavita / LibraForge / Prowlarr after saving stack settings.

    Always returns HTTP 200 with ``ok: true`` and a ``warnings`` list so the
    wizard can continue even when siblings are temporarily down.
    """
    from app.services import instance_settings as inst

    return await inst.validate_setup_connections()


@router.post("/setup-defaults")
async def post_setup_defaults(_admin: User = Depends(require_admin)):
    """Apply recommended RSS-only scraper defaults (safe for Pi)."""
    from app.services import instance_settings as inst

    await inst.apply_setup_defaults()
    return await inst.setup_status()


@router.get("/ol-catalog")
async def get_ol_catalog_status(
    check: bool = False,
    _admin: User = Depends(require_admin),
):
    """Status of the local Open Library catalog DB / build job.

    Pass ``check=true`` to run a throttled lightweight remote dump probe
    (no download). Daily background checks also set ``new_dumps_available``.
    """
    from app.services import ol_catalog_build

    if check:
        return await ol_catalog_build.check_for_updates(force=False, notify=True)
    return ol_catalog_build.get_status()


@router.post("/ol-catalog/check")
async def check_ol_catalog_dumps(_admin: User = Depends(require_admin)):
    """Force a HEAD/etag check for newer Open Library dumps (no download)."""
    from app.services import ol_catalog_build

    return await ol_catalog_build.check_for_updates(force=True, notify=True)


class OlCatalogBuildBody(BaseModel):
    include_editions: bool = False
    skip_download: bool = False
    force_download: bool = False


class OlCatalogScheduleBody(BaseModel):
    """Schedule a future force-download catalog update.

    ``scheduled_at`` is ISO-8601. Prefer an explicit offset or ``Z`` (UTC).
    Naive timestamps are treated as UTC. The Admin UI sends UTC derived from
    the browser's local ``datetime-local`` value.
    """

    scheduled_at: str
    include_editions: bool = False
    force_download: bool = True


@router.post("/ol-catalog/build")
async def start_ol_catalog_build(
    body: OlCatalogBuildBody | None = None,
    _admin: User = Depends(require_admin),
):
    """Start (or report) a long-running Open Library dump import.

    Warning: multi-GB download and multi-hour build on a Pi. Opt-in only.
    Set ``force_download`` (Update catalog) to re-fetch dumps then rebuild.
    """
    from app.services import ol_catalog_build

    opts = body or OlCatalogBuildBody()
    return await ol_catalog_build.start_build(
        include_editions=bool(opts.include_editions),
        skip_download=bool(opts.skip_download),
        force_download=bool(opts.force_download),
    )


@router.post("/ol-catalog/schedule")
async def schedule_ol_catalog_build(
    body: OlCatalogScheduleBody,
    _admin: User = Depends(require_admin),
):
    """Schedule a one-shot dump download + rebuild for a future time.

    Persisted in ``ol_catalog_build.json`` (survives restarts). Does not run
    until the due time; cancel with DELETE ``/ol-catalog/schedule``.
    """
    from app.services import ol_catalog_build

    try:
        return await ol_catalog_build.schedule_build(
            scheduled_at=body.scheduled_at,
            include_editions=bool(body.include_editions),
            force_download=bool(body.force_download),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/ol-catalog/schedule")
async def cancel_ol_catalog_schedule(_admin: User = Depends(require_admin)):
    """Cancel a pending scheduled Open Library catalog update."""
    from app.services import ol_catalog_build

    return await ol_catalog_build.cancel_scheduled_build()


# --- Library Sweep ---


@router.post("/library-sweep/start")
async def library_sweep_start(admin: User = Depends(require_admin)):
    """Start or resume Library Sweep (ABS → forge, no download)."""
    from app.services import library_sweep

    return await library_sweep.start_sweep(user_id=admin.id)


@router.post("/library-sweep/pause")
async def library_sweep_pause(_admin: User = Depends(require_admin)):
    from app.services import library_sweep

    return await library_sweep.pause_sweep()


@router.post("/library-sweep/cancel")
async def library_sweep_cancel(_admin: User = Depends(require_admin)):
    from app.services import library_sweep

    return await library_sweep.cancel_sweep()


@router.get("/library-sweep/status")
async def library_sweep_status(_admin: User = Depends(require_admin)):
    from app.services import library_sweep

    return await library_sweep.get_status()


@router.get("/library-sweep/needs-review")
async def library_sweep_needs_review(_admin: User = Depends(require_admin)):
    from app.services import library_sweep

    items = await library_sweep.list_needs_review()
    status = await library_sweep.get_status()
    return {
        "items": items,
        "review_cursor_request_id": status.get("review_cursor_request_id"),
        "count": len(items),
    }


@router.put("/library-sweep/review-cursor")
async def library_sweep_review_cursor(
    body: SweepReviewCursorBody,
    _admin: User = Depends(require_admin),
):
    from app.services import library_sweep

    return await library_sweep.set_review_cursor(body.request_id)


@router.post("/library-sweep/skip")
async def library_sweep_skip(
    body: SweepSkipBody = Body(default_factory=SweepSkipBody),
    _admin: User = Depends(require_admin),
):
    """Skip the current (or specified) sweep book without cancelling the whole job."""
    from app.services import library_sweep

    return await library_sweep.skip_current(request_id=body.request_id)


@router.get("/library-sweep/unprocessed")
async def library_sweep_unprocessed(_admin: User = Depends(require_admin)):
    """Cancelled / failed / skipped sweep books for manual reprocess."""
    from app.services import library_sweep

    items = await library_sweep.list_unprocessed()
    status = await library_sweep.get_status()
    return {
        "items": items,
        "count": len(items),
        "counts": status.get("unprocessed") or {},
    }


@router.get("/library-sweep/processed")
async def library_sweep_processed(
    limit: int = 50,
    offset: int = 0,
    _admin: User = Depends(require_admin),
):
    """Successfully completed Library Sweep books (paginated)."""
    from app.services import library_sweep

    return await library_sweep.list_processed(limit=limit, offset=offset)


@router.post("/library-sweep/reprocess/{request_id}")
async def library_sweep_reprocess(
    request_id: int,
    admin: User = Depends(require_admin),
):
    """Re-kick forge for an unprocessed sweep book (no debrid)."""
    from app.services import library_sweep

    try:
        return await library_sweep.reprocess_unprocessed(
            request_id, user_id=admin.id
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class SweepDismissBody(BaseModel):
    """Dismiss one or more Unprocessed sweep rows (optional bulk)."""
    request_ids: list[int] = []


@router.post("/library-sweep/dismiss/{request_id}")
async def library_sweep_dismiss_one(
    request_id: int,
    _admin: User = Depends(require_admin),
):
    """Remove a single book from the Unprocessed queue (keeps library files)."""
    from app.services import library_sweep

    try:
        return await library_sweep.dismiss_unprocessed(request_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/library-sweep/dismiss")
async def library_sweep_dismiss_bulk(
    body: SweepDismissBody,
    _admin: User = Depends(require_admin),
):
    """Bulk-remove books from the Unprocessed queue."""
    from app.services import library_sweep

    ids = list(body.request_ids or [])
    if not ids:
        raise HTTPException(status_code=400, detail="request_ids required")
    try:
        return await library_sweep.dismiss_unprocessed(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# --- Library Sweep: Ebooks ---


@router.post("/library-sweep/ebook/start")
async def library_sweep_ebook_start(admin: User = Depends(require_admin)):
    """Start or resume Ebook Sweep (DIY organizer, no download)."""
    from app.services import library_ebook_sweep

    return await library_ebook_sweep.start_sweep(user_id=admin.id)


@router.post("/library-sweep/ebook/pause")
async def library_sweep_ebook_pause(_admin: User = Depends(require_admin)):
    from app.services import library_ebook_sweep

    return await library_ebook_sweep.pause_sweep()


@router.post("/library-sweep/ebook/cancel")
async def library_sweep_ebook_cancel(_admin: User = Depends(require_admin)):
    from app.services import library_ebook_sweep

    return await library_ebook_sweep.cancel_sweep()


@router.get("/library-sweep/ebook/status")
async def library_sweep_ebook_status(_admin: User = Depends(require_admin)):
    from app.services import library_ebook_sweep

    return await library_ebook_sweep.get_status()


@router.get("/library-sweep/ebook/needs-review")
async def library_sweep_ebook_needs_review(_admin: User = Depends(require_admin)):
    from app.services import library_ebook_sweep

    items = await library_ebook_sweep.list_needs_review()
    status = await library_ebook_sweep.get_status()
    return {
        "items": items,
        "review_cursor_request_id": status.get("review_cursor_request_id"),
        "count": len(items),
    }


@router.put("/library-sweep/ebook/review-cursor")
async def library_sweep_ebook_review_cursor(
    body: SweepReviewCursorBody,
    _admin: User = Depends(require_admin),
):
    from app.services import library_ebook_sweep

    return await library_ebook_sweep.set_review_cursor(body.request_id)


@router.post("/library-sweep/ebook/skip")
async def library_sweep_ebook_skip(
    body: SweepSkipBody = Body(default_factory=SweepSkipBody),
    _admin: User = Depends(require_admin),
):
    """Skip the current (or specified) ebook sweep book without cancelling the job."""
    from app.services import library_ebook_sweep

    return await library_ebook_sweep.skip_current(request_id=body.request_id)


@router.get("/library-sweep/ebook/unprocessed")
async def library_sweep_ebook_unprocessed(_admin: User = Depends(require_admin)):
    """Cancelled / failed / skipped ebook sweep books for manual reprocess."""
    from app.services import library_ebook_sweep

    items = await library_ebook_sweep.list_unprocessed()
    status = await library_ebook_sweep.get_status()
    return {
        "items": items,
        "count": len(items),
        "counts": status.get("unprocessed") or {},
    }


@router.get("/library-sweep/ebook/processed")
async def library_sweep_ebook_processed(
    limit: int = 50,
    offset: int = 0,
    _admin: User = Depends(require_admin),
):
    """Successfully completed Ebook Sweep books (paginated)."""
    from app.services import library_ebook_sweep

    return await library_ebook_sweep.list_processed(limit=limit, offset=offset)


@router.post("/library-sweep/ebook/reprocess/{request_id}")
async def library_sweep_ebook_reprocess(
    request_id: int,
    admin: User = Depends(require_admin),
):
    """Re-kick the DIY ebook pipeline for an unprocessed sweep book (no debrid)."""
    from app.services import library_ebook_sweep

    try:
        return await library_ebook_sweep.reprocess_unprocessed(
            request_id, user_id=admin.id
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/library-sweep/ebook/dismiss/{request_id}")
async def library_sweep_ebook_dismiss_one(
    request_id: int,
    _admin: User = Depends(require_admin),
):
    """Remove a single book from the Ebook Sweep Unprocessed queue (keeps library files)."""
    from app.services import library_ebook_sweep

    try:
        return await library_ebook_sweep.dismiss_unprocessed(request_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/library-sweep/ebook/dismiss")
async def library_sweep_ebook_dismiss_bulk(
    body: SweepDismissBody,
    _admin: User = Depends(require_admin),
):
    """Bulk-remove books from the Ebook Sweep Unprocessed queue."""
    from app.services import library_ebook_sweep

    ids = list(body.request_ids or [])
    if not ids:
        raise HTTPException(status_code=400, detail="request_ids required")
    try:
        return await library_ebook_sweep.dismiss_unprocessed(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# --- Library Sweep: Cleanup ---


class SweepCleanupPreviewBody(BaseModel):
    """Dry-run preview scopes for orphan cleanup (default both media types)."""
    scopes: list[str] | None = None


class SweepCleanupApplyBody(BaseModel):
    """Confirm token + optional path subset from a prior cleanup preview."""
    token: str
    paths: list[str] | None = None


@router.post("/library-sweep/cleanup/preview")
async def library_sweep_cleanup_preview(
    body: SweepCleanupPreviewBody = Body(default_factory=SweepCleanupPreviewBody),
    _admin: User = Depends(require_admin),
):
    """Dry-run preview of non-canonical leftovers under the library roots."""
    from app.services import library_folder_cleanup

    scopes = body.scopes or ["audiobook", "ebook"]
    invalid = [s for s in scopes if s not in ("audiobook", "ebook")]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid scope(s): {invalid}")
    preview = library_folder_cleanup.classify_library_orphans(scopes=scopes)
    return preview.to_dict()


@router.post("/library-sweep/cleanup/apply")
async def library_sweep_cleanup_apply(
    body: SweepCleanupApplyBody,
    _admin: User = Depends(require_admin),
):
    """Delete previously previewed orphan paths (subset allowed)."""
    from app.services import library_folder_cleanup

    try:
        return library_folder_cleanup.apply_cleanup(token=body.token, paths=body.paths)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/library-sweep/cleanup/canonical")
async def library_sweep_cleanup_canonical(_admin: User = Depends(require_admin)):
    """Documented canonical library layout (audiobook + ebook)."""
    from app.services import library_folder_cleanup

    return library_folder_cleanup.canonical_layout_docs()


# --- Launch ops: backups, activity, shares, whitelist ---

@router.get("/backups")
async def list_backups(_admin: User = Depends(require_admin)):
    from app.services import db_backup

    try:
        targets = db_backup.backup_targets()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"targets": targets}


@router.post("/backups/now")
@router.post("/backups/{target_id}")
async def run_backup_now(
    target_id: str = "app.db",
    _admin: User = Depends(require_admin),
):
    from app.services import db_backup

    if target_id not in ("app.db", "now", ""):
        # Accept path-style ids from the UI; only app.db is supported today.
        if target_id != "app.db":
            raise HTTPException(status_code=404, detail="Unknown backup target")
    try:
        result = db_backup.create_backup_now()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "backup": result, "targets": db_backup.backup_targets()}


@router.get("/activity")
async def admin_activity(
    limit: int = 100,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Chronological feed of meaningful server activity for admins."""
    from app.models import BookShare

    lim = max(1, min(int(limit or 100), 300))
    events: list[dict] = []

    users = (
        await db.execute(select(User).order_by(User.created_at.desc()).limit(lim))
    ).scalars().all()
    for u in users:
        events.append(
            {
                "id": f"user-join-{u.id}",
                "kind": "user_joined",
                "type": "user_joined",
                "title": f"{u.username} joined",
                "message": f"{u.username} created an account",
                "username": u.username,
                "created_at": _iso(u.created_at),
                "at": _iso(u.created_at),
            }
        )

    reqs = (
        await db.execute(
            select(DownloadRequest).order_by(DownloadRequest.created_at.desc()).limit(lim)
        )
    ).scalars().all()
    user_ids = {r.user_id for r in reqs}
    unames: dict[int, str] = {}
    if user_ids:
        for u in (
            await db.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars().all():
            unames[u.id] = u.username
    for r in reqs:
        status = (r.status or "").lower()
        kind = "request_created"
        msg = f"Requested {r.title}"
        if status == "completed":
            kind = "request_completed"
            msg = f"Completed {r.title}"
        elif status in ("failed", "admin_rejected", "quarantined"):
            kind = "request_failed"
            msg = f"{status.replace('_', ' ').title()}: {r.title}"
        ts = r.completed_at or r.created_at
        events.append(
            {
                "id": f"req-{r.id}-{status}",
                "kind": kind,
                "type": kind,
                "title": msg,
                "message": msg,
                "username": unames.get(r.user_id),
                "created_at": _iso(ts),
                "at": _iso(ts),
                "meta": {"request_id": r.id, "status": r.status, "media_type": r.media_type},
            }
        )

    streams = (
        await db.execute(
            select(StreamHistory).order_by(StreamHistory.updated_at.desc()).limit(lim)
        )
    ).scalars().all()
    stream_uids = {s.user_id for s in streams}
    if stream_uids:
        for u in (
            await db.execute(select(User).where(User.id.in_(stream_uids)))
        ).scalars().all():
            unames[u.id] = u.username
    for s in streams:
        status = (s.status or "").lower()
        if status == "playing":
            kind, verb = "stream_started", "started listening to"
        elif status == "finished":
            kind, verb = "stream_ended", "finished"
        elif status == "paused":
            kind, verb = "stream_paused", "paused"
        else:
            kind, verb = "stream_activity", "streamed"
        msg = f"{verb} {s.title}"
        events.append(
            {
                "id": f"stream-{s.id}-{status}",
                "kind": kind,
                "type": kind,
                "title": msg,
                "message": msg,
                "username": unames.get(s.user_id),
                "created_at": _iso(s.updated_at or s.created_at),
                "at": _iso(s.updated_at or s.created_at),
            }
        )

    shares = (
        await db.execute(select(BookShare).order_by(BookShare.created_at.desc()).limit(lim))
    ).scalars().all()
    share_uids = {s.created_by_user_id for s in shares}
    if share_uids:
        for u in (
            await db.execute(select(User).where(User.id.in_(share_uids)))
        ).scalars().all():
            unames[u.id] = u.username
    for s in shares:
        title = s.title or s.abs_item_id or f"series {s.kavita_series_id}" or "book"
        events.append(
            {
                "id": f"share-create-{s.id}",
                "kind": "share_created",
                "type": "share_created",
                "title": f"Shared {title}",
                "message": f"Shared {title}",
                "username": unames.get(s.created_by_user_id),
                "created_at": _iso(s.created_at),
                "at": _iso(s.created_at),
            }
        )
        if s.revoked_at is not None:
            events.append(
                {
                    "id": f"share-revoke-{s.id}",
                    "kind": "share_revoked",
                    "type": "share_revoked",
                    "title": f"Revoked share of {title}",
                    "message": f"Revoked share of {title}",
                    "username": unames.get(s.created_by_user_id),
                    "created_at": _iso(s.revoked_at),
                    "at": _iso(s.revoked_at),
                }
            )

    def _sort_key(ev: dict):
        return ev.get("at") or ev.get("created_at") or ""

    events.sort(key=_sort_key, reverse=True)
    events = events[:lim]
    return {"events": events, "activity": events, "items": events}


@router.get("/shares")
async def list_shares(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.models import BookShare
    from app.routers.share import _public_share_url

    rows = (
        await db.execute(
            select(BookShare)
            .where(BookShare.revoked_at.is_(None))
            .order_by(BookShare.created_at.desc())
        )
    ).scalars().all()
    uids = {r.created_by_user_id for r in rows}
    unames: dict[int, str] = {}
    if uids:
        for u in (
            await db.execute(select(User).where(User.id.in_(uids)))
        ).scalars().all():
            unames[u.id] = u.username
    out = []
    for r in rows:
        path = f"/share/{r.token}"
        out.append(
            {
                "id": r.id,
                "token": r.token,
                "media_type": r.media_type or "audiobook",
                "title": r.title or r.abs_item_id or f"Series {r.kavita_series_id}",
                "abs_item_id": r.abs_item_id,
                "kavita_series_id": r.kavita_series_id,
                "kavita_chapter_id": r.kavita_chapter_id,
                "created_by": unames.get(r.created_by_user_id),
                "username": unames.get(r.created_by_user_id),
                "created_at": _iso(r.created_at),
                "revoked_at": _iso(r.revoked_at),
                "path": path,
                "url": await _public_share_url(path),
            }
        )
    return {"shares": out}


@router.delete("/shares/{share_id}")
async def revoke_share(
    share_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.models import BookShare

    row = None
    if share_id.isdigit():
        row = (
            await db.execute(select(BookShare).where(BookShare.id == int(share_id)))
        ).scalar_one_or_none()
    if row is None:
        row = (
            await db.execute(select(BookShare).where(BookShare.token == share_id))
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Share not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    return {"ok": True, "id": row.id, "revoked_at": _iso(row.revoked_at)}


@router.post("/shares/revoke-all")
async def revoke_all_shares(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.models import BookShare

    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(select(BookShare).where(BookShare.revoked_at.is_(None)))
    ).scalars().all()
    for r in rows:
        r.revoked_at = now
    await db.commit()
    return {"ok": True, "revoked": len(rows)}


@router.get("/libraforge-whitelist")
async def get_libraforge_whitelist(_admin: User = Depends(require_admin)):
    from app.services import admin_whitelist

    return admin_whitelist.read_whitelist()


@router.post("/libraforge-whitelist/sync")
async def sync_libraforge_whitelist(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services import admin_whitelist

    return await admin_whitelist.sync_admin_ips(db)