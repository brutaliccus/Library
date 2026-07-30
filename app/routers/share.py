"""Public book share links - guests listen/read one book via token URL."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BookShare, User
from app.services import audiobookshelf, kavita
from app.utils.auth import get_current_user
from app.utils.book_series import is_junk_series_hint

router = APIRouter(prefix="/api/share", tags=["share"])


async def _public_share_url(path: str) -> str:
    """Build an absolute share URL from Admin APP_URL (never localhost)."""
    base = ""
    try:
        from app.services import instance_settings
        base = (await instance_settings.get_effective("config.app_url")).strip()
    except Exception:
        base = ""
    if not base:
        from app.config import get_settings
        base = (get_settings().app_url or "").strip()
    base = base.rstrip("/")
    if not base:
        return path
    return f"{base}{path if path.startswith('/') else '/' + path}"


class CreateShareBody(BaseModel):
    item_id: str | None = None
    media_type: str = "audiobook"
    series_id: int | None = None
    chapter_id: int | None = None
    title: str | None = None


def _user_may_share(user: User) -> bool:
    if (user.role or "").lower() == "admin":
        return True
    return bool(getattr(user, "can_share_books", False))


async def _get_active_share(db: AsyncSession, token: str) -> BookShare:
    tok = (token or "").strip()
    if not tok or len(tok) < 16:
        raise HTTPException(status_code=404, detail="Share link not found")
    row = (await db.execute(select(BookShare).where(BookShare.token == tok))).scalar_one_or_none()
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
        "publishedYear": str(
            ((item.get("media") or {}).get("metadata") or {}).get("publishedYear") or ""
        ),
        "genres": abs_genres,
        "series": out_series,
        "duration": float(normalized.get("duration") or 0),
        "numTracks": int(normalized.get("numTracks") or 0),
        "coverUrl": f"/api/stream/abs/proxy/cover/{item.get('id') or item_id}",
    }


async def _ebook_detail_payload(share: BookShare) -> dict:
    series_id = share.kavita_series_id
    chapter_id = share.kavita_chapter_id
    if not series_id:
        raise HTTPException(status_code=404, detail="Book not found")

    title = (share.title or "").strip()
    author = ""
    description = ""
    genres: list[str] = []
    series_out: list[dict] = []

    try:
        series_list = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
        series = next((s for s in series_list if s.get("id") == series_id), None)
        if series:
            title = title or (
                series.get("name")
                or series.get("localizedName")
                or series.get("originalName")
                or ""
            )
        meta = await kavita.get_series_metadata(series_id)
        meta = meta or {}
        writers = meta.get("writers") or (series or {}).get("authors") or []
        if writers:
            author = (
                (writers[0] or {}).get("name", "")
                if isinstance(writers[0], dict)
                else str(writers[0])
            )
        description = (meta.get("summary") or meta.get("description") or "").strip()
        genres = _normalize_item_genres(meta.get("genres") or [])
    except Exception:
        pass

    if not chapter_id:
        try:
            volumes = await kavita.get_series_volumes(series_id)
            for vol in volumes or []:
                chapters = vol.get("chapters") or []
                if chapters:
                    chapter_id = chapters[0].get("id")
                    break
        except Exception:
            chapter_id = None

    cover_url = f"/api/library/reader/cover/ebook?seriesId={series_id}"
    if chapter_id:
        cover_url += f"&chapterId={chapter_id}"

    return {
        "itemId": "",
        "media_type": "ebook",
        "seriesId": series_id,
        "series_id": series_id,
        "chapterId": chapter_id,
        "chapter_id": chapter_id,
        "title": title or "Ebook",
        "subtitle": "",
        "author": author,
        "narrator": "",
        "description": description,
        "publisher": "",
        "publishedYear": "",
        "genres": genres,
        "series": series_out,
        "duration": 0,
        "numTracks": 0,
        "coverUrl": cover_url,
    }

@router.post("")
async def create_share(
    body: CreateShareBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create (or reuse) a public share link for one audiobook or ebook."""
    if not _user_may_share(user):
        raise HTTPException(status_code=403, detail="Sharing is not enabled for your account")

    media_type = (body.media_type or "audiobook").strip().lower()
    if media_type not in ("audiobook", "ebook"):
        raise HTTPException(status_code=400, detail="media_type must be audiobook or ebook")

    if media_type == "ebook":
        series_id = body.series_id
        chapter_id = body.chapter_id
        if not series_id:
            raise HTTPException(status_code=400, detail="series_id is required for ebooks")
        title = (body.title or "").strip()[:512] or None

        existing = (
            await db.execute(
                select(BookShare).where(
                    BookShare.created_by_user_id == user.id,
                    BookShare.media_type == "ebook",
                    BookShare.kavita_series_id == series_id,
                    BookShare.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing:
            path = f"/share/{existing.token}"
            return {
                "token": existing.token,
                "media_type": "ebook",
                "seriesId": existing.kavita_series_id,
                "chapterId": existing.kavita_chapter_id,
                "path": path,
                "url": await _public_share_url(path),
            }

        token = secrets.token_urlsafe(24)
        row = BookShare(
            token=token,
            media_type="ebook",
            abs_item_id=None,
            kavita_series_id=series_id,
            kavita_chapter_id=chapter_id,
            title=title,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.commit()
        path = f"/share/{token}"
        return {
            "token": token,
            "media_type": "ebook",
            "seriesId": series_id,
            "chapterId": chapter_id,
            "path": path,
            "url": await _public_share_url(path),
        }

    item_id = (body.item_id or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="item_id is required")

    item = await audiobookshelf.get_library_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Book not found")

    normalized = audiobookshelf._normalize_abs_item(item)
    title = (normalized.get("title") or "").strip()[:512] or None

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
        path = f"/share/{existing.token}"
        return {
            "token": existing.token,
            "media_type": "audiobook",
            "itemId": existing.abs_item_id,
            "path": path,
            "url": await _public_share_url(path),
        }

    token = secrets.token_urlsafe(24)
    row = BookShare(
        token=token,
        media_type="audiobook",
        abs_item_id=item_id,
        title=title,
        created_by_user_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    path = f"/share/{token}"
    return {
        "token": token,
        "media_type": "audiobook",
        "itemId": item_id,
        "path": path,
        "url": await _public_share_url(path),
    }


@router.get("/{token}")
async def resolve_share(token: str, db: AsyncSession = Depends(get_db)):
    """Public: resolve a share token to book details metadata."""
    share = await _get_active_share(db, token)
    media_type = (share.media_type or "audiobook").strip().lower()
    if media_type == "ebook":
        detail = await _ebook_detail_payload(share)
        return {"token": share.token, "guest": True, **detail}
    if not share.abs_item_id:
        raise HTTPException(status_code=404, detail="Share link not found")
    detail = await _item_detail_payload(share.abs_item_id)
    return {
        "token": share.token,
        "itemId": share.abs_item_id,
        "media_type": "audiobook",
        "guest": True,
        **detail,
    }


@router.get("/{token}/chapters")
async def share_chapters(token: str, db: AsyncSession = Depends(get_db)):
    share = await _get_active_share(db, token)
    if (share.media_type or "audiobook") == "ebook":
        raise HTTPException(status_code=404, detail="Chapters not available for ebooks")
    chapters = await audiobookshelf.get_item_chapters(share.abs_item_id)
    if chapters is None:
        raise HTTPException(status_code=404, detail="Library item not found")
    return {"chapters": chapters}


@router.get("/{token}/offline")
async def share_offline(token: str, db: AsyncSession = Depends(get_db)):
    """Public: track URLs for guest listen / Save offline (no ABS play tracking)."""
    share = await _get_active_share(db, token)
    if (share.media_type or "audiobook") == "ebook":
        raise HTTPException(status_code=404, detail="Offline audio not available for ebooks")
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
    """Public: start guest playback - same track payload as offline, no server progress."""
    return await share_offline(token, db)


@router.get("/{token}/ebook/book-info")
async def share_ebook_book_info(token: str, db: AsyncSession = Depends(get_db)):
    """Public: Kavita book-info for guest Read."""
    share = await _get_active_share(db, token)
    if (share.media_type or "") != "ebook" or not share.kavita_chapter_id:
        raise HTTPException(status_code=404, detail="Ebook share not found")
    info = await kavita.get_book_info(share.kavita_chapter_id)
    if not info:
        raise HTTPException(status_code=404, detail="Book not found")
    return info


@router.get("/{token}/ebook/chapters")
async def share_ebook_chapters(token: str, db: AsyncSession = Depends(get_db)):
    share = await _get_active_share(db, token)
    if (share.media_type or "") != "ebook" or not share.kavita_chapter_id:
        raise HTTPException(status_code=404, detail="Ebook share not found")
    chapters = await kavita.get_book_chapters(share.kavita_chapter_id)
    return chapters or []


@router.get("/{token}/ebook/book-page")
async def share_ebook_book_page(
    token: str,
    page: int = 0,
    db: AsyncSession = Depends(get_db),
):
    share = await _get_active_share(db, token)
    if (share.media_type or "") != "ebook" or not share.kavita_chapter_id:
        raise HTTPException(status_code=404, detail="Ebook share not found")
    html = await kavita.get_book_page(share.kavita_chapter_id, page)
    if html is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return Response(content=html, media_type="text/html; charset=utf-8")