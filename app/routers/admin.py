import asyncio
import logging
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, AccountRequest, DownloadRequest
from app.utils.auth import require_admin, hash_password
from app.services import push, real_debrid, audiobookshelf, kavita, downloader, goodreads
from app.services.pipeline import (
    audiobook_destination_dir,
    organize_audiobook_files,
    _is_collection_title,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)


class ApproveRequest(BaseModel):
    password: str | None = None


class DenyRequest(BaseModel):
    reason: str | None = None


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class AccountRequestResponse(BaseModel):
    id: int
    username: str
    email: str | None
    reason: str | None
    status: str
    created_at: str

    model_config = {"from_attributes": True}


class AdminDownloadResponse(BaseModel):
    id: int
    title: str
    author: str | None
    status: str
    status_detail: str | None
    username: str
    created_at: str
    completed_at: str | None
    progress_percent: float | None = None
    progress_bytes: int | None = None
    progress_total_bytes: int | None = None
    progress_speed_bps: float | None = None


# --- Account Approvals ---

@router.get("/account-requests", response_model=list[AccountRequestResponse])
async def list_account_requests(
    status_filter: str | None = None,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(AccountRequest).order_by(AccountRequest.created_at.desc())
    if status_filter:
        query = query.where(AccountRequest.status == status_filter)
    result = await db.execute(query)
    reqs = result.scalars().all()
    return [
        AccountRequestResponse(
            id=r.id,
            username=r.username,
            email=r.email,
            reason=r.reason,
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in reqs
    ]


@router.post("/account-requests/{request_id}/approve")
async def approve_account(
    request_id: int,
    body: ApproveRequest,
    background_tasks: BackgroundTasks,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AccountRequest).where(AccountRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req or req.status != "pending":
        raise HTTPException(status_code=404, detail="Pending request not found")

    existing = await db.execute(select(User).where(User.username == req.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    default_password = "changeme"

    user = User(
        username=req.username,
        hashed_password=hash_password(default_password),
        role="user",
        must_change_password=True,
    )
    db.add(user)

    req.status = "approved"
    req.resolved_at = datetime.now(timezone.utc)
    await db.commit()

    background_tasks.add_task(
        push.notify_admins_background,
        {"type": "account_approved", "title": "Account Approved", "body": f"{req.username} can now log in", "url": "/admin?tab=users"},
    )

    return {"message": f"Account created for {req.username}"}


@router.post("/account-requests/{request_id}/deny")
async def deny_account(
    request_id: int,
    body: DenyRequest,
    background_tasks: BackgroundTasks,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AccountRequest).where(AccountRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req or req.status != "pending":
        raise HTTPException(status_code=404, detail="Pending request not found")

    req.status = "denied"
    req.deny_reason = body.reason
    req.resolved_at = datetime.now(timezone.utc)
    await db.commit()

    background_tasks.add_task(
        push.notify_admins_background,
        {
            "type": "account_denied",
            "title": "Account Denied",
            "body": f"{req.username}" + (f": {body.reason}" if body.reason else ""),
            "url": "/admin?tab=approvals",
        },
    )

    return {"message": f"Account request for {req.username} denied"}


# --- User Management ---

@router.get("/users", response_model=list[UserResponse])
async def list_users(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return [
        UserResponse(
            id=u.id,
            username=u.username,
            role=u.role,
            is_active=u.is_active,
            created_at=u.created_at.isoformat() if u.created_at else "",
        )
        for u in users
    ]


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    await db.commit()
    return {"message": f"User {user.username} disabled"}


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

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
    result = await db.execute(
        select(DownloadRequest, User.username)
        .join(User, DownloadRequest.user_id == User.id)
        .order_by(DownloadRequest.created_at.desc())
    )
    return [
        AdminDownloadResponse(
            id=req.id,
            title=req.title,
            author=req.author,
            status=req.status,
            status_detail=req.status_detail,
            username=username,
            created_at=req.created_at.isoformat() if req.created_at else "",
            completed_at=req.completed_at.isoformat() if req.completed_at else None,
            progress_percent=req.progress_percent,
            progress_bytes=req.progress_bytes,
            progress_total_bytes=req.progress_total_bytes,
            progress_speed_bps=req.progress_speed_bps,
        )
        for req, username in result.all()
    ]


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
    import shutil

    rd_info = None
    try:
        rd_info = await real_debrid.get_user_info()
    except Exception:
        pass

    torbox_info = None
    from app.services import torbox
    from app.config import get_settings as _gs
    if _gs().torbox_api_token:
        try:
            torbox_info = await torbox.get_user_info()
        except Exception:
            pass

    abs_ok = await audiobookshelf.health_check()
    kavita_ok = await kavita.health_check()

    from app.config import get_settings
    settings = get_settings()

    disk = shutil.disk_usage(settings.audiobook_dir)

    return {
        "real_debrid": {
            "connected": rd_info is not None,
            "username": rd_info.get("username") if rd_info else None,
            "premium": rd_info.get("premium") if rd_info else None,
            "points": rd_info.get("points") if rd_info else None,
        },
        "torbox": {
            "configured": bool(_gs().torbox_api_token),
            "connected": torbox_info is not None,
            "username": (torbox_info or {}).get("email") or (torbox_info or {}).get("customer"),
            "plan": (torbox_info or {}).get("plan"),
        },
        "audiobookshelf": {
            "connected": abs_ok,
            "url": settings.abs_url,
        },
        "kavita": {
            "connected": kavita_ok,
            "url": settings.kavita_url,
        },
        "disk": {
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round(disk.used / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
        },
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
    from app.services import indexer_scraper
    await indexer_scraper.clear_error()
    return await indexer_scraper.get_status()


class ScraperSettingsUpdate(BaseModel):
    # Partial update: any subset of the fields declared in scraper_settings.FIELDS.
    updates: dict[str, int | str]


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
