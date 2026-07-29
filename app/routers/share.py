"""Public book share links — guests listen to one ABS audiobook via token URL."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BookShare, User
from app.services import audiobookshelf
from app.utils.book_series import is_junk_series_hint
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/share", tags=["share"])


class CreateShareBody(BaseModel):
    item_id: str


def _user_may_share(user: User) -> bool:
    if (user.role or "").lower() == "admin":
        return True
    return bool(getattr(user, "can_share_books", False))


async def _get_active_share(db: AsyncSession, token: str) -> BookShare:
    tok = (token or "").strip()
    if not tok or len(tok) < 16:
        raise HTTPException(status_code=404, detail="Share link not found")
    row = (
        await db.execute(select(BookShare).where(BookShare.token == tok))
    ).scalar_one_or_none()
    if not row or row.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Share link not found")
    return row


def _normalize_item_genres(raw: list | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for g in raw or []:
        s = str(g or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


async def _item_detail_payload(item_id: str) -> dict:
    item = await audiobookshelf.get_library_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Book not found")
    normalized = audiobookshelf._normalize_abs_item(item)
    title = normalized.get("title") or ""
    author = (normalized.get("author") or "").strip()
    abs_genres = _normalize_item_genres(normalized.get("genres") or [])
    sname = (normalized.get("seriesName") or "").strip()
    seq = str(normalized.get("sequence") or "").strip()
    if sname:
        out_series = [{"id": "", "name": sname, "sequence": seq}]
    else:
        out_series = [
            s
            for s in (normalized.get("series") or [])
            if (s.get("name") or "").strip() and not is_junk_series_hint(s.get("name") or "")
        ]

    return {
        "itemId": item.get("id", "") or normalized.get("itemId", ""),
        "title": title,
        "subtitle": normalized.get("subtitle") or "",
        "author": author,
        "narrator": normalized.get("narrator") or "",
        "description": normalized.get("description") or "",
        "publisher": ((item.get("media") or {}).get("metadata") or {}).get("publisher") or "",
        "publishedYear": str(((item.get("media") or {}).get("metadata") or {}).get("publishedYear") or ""),
        "genres": abs_genres,
        "series": out_series,
        "duration": float(normalized.get("duration") or 0),
        "numTracks": int(normalized.get("numTracks") or 0),
        "coverUrl": f"/api/stream/abs/proxy/cover/{item.get('id') or item_id}",
    }


@router.post("")
async def create_share(
    body: CreateShareBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create (or reuse) a public share link for one ABS audiobook."""
    if not _user_may_share(user):
        raise HTTPException(status_code=403, detail="Sharing is not enabled for your account")

    item_id = (body.item_id or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="item_id is required")

    item = await audiobookshelf.get_library_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Book not found")

    existing = (
        await db.execute(
            select(BookShare).where(
                BookShare.created_by_user_id == user.id,
                BookShare.abs_item_id == item_id,
                BookShare.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        return {
            "token": existing.token,
            "itemId": existing.abs_item_id,
            "path": f"/share/{existing.token}",
        }

    token = secrets.token_urlsafe(24)
    row = BookShare(
        token=token,
        abs_item_id=item_id,
        created_by_user_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    return {
        "token": token,
        "itemId": item_id,
        "path": f"/share/{token}",
    }


@router.get("/{token}")
async def resolve_share(token: str, db: AsyncSession = Depends(get_db)):
    """Public: resolve a share token to book details metadata."""
    share = await _get_active_share(db, token)
    detail = await _item_detail_payload(share.abs_item_id)
    return {
        "token": share.token,
        "itemId": share.abs_item_id,
        "guest": True,
        **detail,
    }


@router.get("/{token}/chapters")
async def share_chapters(token: str, db: AsyncSession = Depends(get_db)):
    share = await _get_active_share(db, token)
    chapters = await audiobookshelf.get_item_chapters(share.abs_item_id)
    if chapters is None:
        raise HTTPException(status_code=404, detail="Library item not found")
    return {"chapters": chapters}


@router.get("/{token}/offline")
async def share_offline(token: str, db: AsyncSession = Depends(get_db)):
    """Public: track URLs for guest listen / Save offline (no ABS play tracking)."""
    share = await _get_active_share(db, token)
    info = await audiobookshelf.get_offline_download_info(share.abs_item_id)
    if not info or not info.get("tracks"):
        raise HTTPException(status_code=404, detail="No audio tracks found")
    return {
        "sessionId": "",
        "itemId": share.abs_item_id,
        "tracks": info["tracks"],
        "startOffset": 0.0,
        "coverUrl": info.get("coverUrl") or "",
        "title": info.get("title") or "Audiobook",
        "author": info.get("author") or "",
        "duration": float(info.get("duration") or 0),
        "chapters": info.get("chapters") or [],
        "guest": True,
    }


@router.post("/{token}/play")
async def share_play(token: str, db: AsyncSession = Depends(get_db)):
    """Public: start guest playback — same track payload as offline, no server progress."""
    return await share_offline(token, db)
