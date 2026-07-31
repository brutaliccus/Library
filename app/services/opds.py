"""Library Site proxied OPDS feeds for ereaders (KOReader, Moon+, etc.).

Users never see the Kavita API key. Each Library Site account gets a personal
``opds_token``; feeds and downloads are authorized with that token in the URL
(same pattern as Kavita's ``/api/opds/{apiKey}``).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserEreaderItem

logger = logging.getLogger(__name__)

ATOM_NS = "http://www.w3.org/2005/Atom"
OPDS_NS = "http://opds-spec.org/2010/catalog"
DCTERMS_NS = "http://purl.org/dc/terms/"

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".mobi": "application/x-mobipocket-ebook",
    ".azw3": "application/vnd.amazon.ebook",
    ".cbz": "application/vnd.comicbook+zip",
    ".cbr": "application/vnd.comicbook-rar",
}

NAV_TYPE = 'application/atom+xml;profile=opds-catalog;kind=navigation'
ACQ_TYPE = 'application/atom+xml;profile=opds-catalog;kind=acquisition'


def new_opds_token() -> str:
    """Long opaque token for /api/opds/{token} (backward compatible)."""
    return secrets.token_urlsafe(24)


def new_opds_short_code() -> str:
    """Short code for /o/{code} (~11 chars, ~66 bits)."""
    return secrets.token_urlsafe(8)


async def _allocate_unique(
    db: AsyncSession,
    *,
    column,
    factory,
) -> str:
    for _ in range(8):
        candidate = factory()
        existing = (
            await db.execute(select(User.id).where(column == candidate))
        ).scalar_one_or_none()
        if existing is None:
            return candidate
    raise RuntimeError("Could not allocate unique OPDS credential")


async def ensure_user_opds_token(db: AsyncSession, user: User) -> str:
    """Create a stable OPDS token (+ short code) if the user does not have one yet."""
    changed = False
    token = (user.opds_token or "").strip()
    if not token:
        user.opds_token = await _allocate_unique(
            db, column=User.opds_token, factory=new_opds_token
        )
        changed = True
    short = (getattr(user, "opds_short_code", None) or "").strip()
    if not short:
        user.opds_short_code = await _allocate_unique(
            db, column=User.opds_short_code, factory=new_opds_short_code
        )
        changed = True
    if changed:
        await db.commit()
        await db.refresh(user)
    return (user.opds_token or "").strip()


async def ensure_user_opds_short_code(db: AsyncSession, user: User) -> str:
    """Return the user's short OPDS code, creating credentials if needed."""
    await ensure_user_opds_token(db, user)
    return (user.opds_short_code or "").strip()


async def rotate_user_opds_token(db: AsyncSession, user: User) -> str:
    user.opds_token = None
    user.opds_short_code = None
    await db.flush()
    return await ensure_user_opds_token(db, user)


async def user_by_opds_token(db: AsyncSession, token: str) -> User | None:
    """Resolve a user by long token or short code (either may appear in the URL)."""
    tok = (token or "").strip()
    if not tok or len(tok) < 8:
        return None
    by_short = (
        await db.execute(
            select(User).where(User.opds_short_code == tok, User.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if by_short is not None:
        return by_short
    return (
        await db.execute(select(User).where(User.opds_token == tok, User.is_active.is_(True)))
    ).scalar_one_or_none()


async def public_app_base() -> str:
    try:
        from app.services import instance_settings

        base = (await instance_settings.get_effective("config.app_url")).strip()
    except Exception:
        base = ""
    if not base:
        from app.config import get_settings

        base = (get_settings().app_url or "").strip()
    return base.rstrip("/")


def media_type_for_path(path: Path | str) -> str:
    suffix = Path(path).suffix.lower()
    return MEDIA_TYPES.get(suffix, "application/octet-stream")


def _atom_updated(dt: datetime | None = None) -> str:
    when = dt or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _xml_escape(text: str) -> str:
    return escape(text or "", {"\"": "&quot;", "'": "&apos;"})


def _link(rel: str, href: str, type_: str, title: str | None = None) -> str:
    title_attr = f' title="{_xml_escape(title)}"' if title else ""
    return (
        f'<link rel="{_xml_escape(rel)}" href="{_xml_escape(href)}" '
        f'type="{_xml_escape(type_)}"{title_attr}/>'
    )


def build_navigation_feed(
    *,
    feed_id: str,
    title: str,
    self_href: str,
    entries: list[dict[str, str]],
) -> str:
    """Build an OPDS navigation (catalog) Atom feed."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<feed xmlns="{ATOM_NS}" xmlns:opds="{OPDS_NS}">',
        f"<id>{_xml_escape(feed_id)}</id>",
        f"<title>{_xml_escape(title)}</title>",
        f"<updated>{_atom_updated()}</updated>",
        _link("self", self_href, NAV_TYPE),
        _link("start", self_href, NAV_TYPE),
    ]
    for entry in entries:
        parts.append("<entry>")
        parts.append(f"<id>{_xml_escape(entry['id'])}</id>")
        parts.append(f"<title>{_xml_escape(entry['title'])}</title>")
        parts.append(f"<updated>{_atom_updated()}</updated>")
        if entry.get("summary"):
            parts.append(f"<content type=\"text\">{_xml_escape(entry['summary'])}</content>")
        parts.append(_link("subsection", entry["href"], entry.get("type", ACQ_TYPE)))
        parts.append("</entry>")
    parts.append("</feed>")
    return "\n".join(parts)


def build_acquisition_feed(
    *,
    feed_id: str,
    title: str,
    self_href: str,
    root_href: str,
    books: list[dict[str, Any]],
) -> str:
    """Build an OPDS acquisition feed listing downloadable ebooks."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<feed xmlns="{ATOM_NS}" xmlns:opds="{OPDS_NS}" xmlns:dcterms="{DCTERMS_NS}">',
        f"<id>{_xml_escape(feed_id)}</id>",
        f"<title>{_xml_escape(title)}</title>",
        f"<updated>{_atom_updated()}</updated>",
        _link("self", self_href, ACQ_TYPE),
        _link("start", root_href, NAV_TYPE),
        _link("up", root_href, NAV_TYPE),
    ]
    for book in books:
        parts.append("<entry>")
        parts.append(f"<id>{_xml_escape(str(book['id']))}</id>")
        parts.append(f"<title>{_xml_escape(book.get('title') or 'Ebook')}</title>")
        parts.append(f"<updated>{_atom_updated(book.get('updated'))}</updated>")
        author = (book.get("author") or "").strip()
        if author:
            parts.append(f"<author><name>{_xml_escape(author)}</name></author>")
        summary = (book.get("summary") or "").strip()
        if summary:
            parts.append(f"<summary>{_xml_escape(summary[:500])}</summary>")
        cover = (book.get("cover_href") or "").strip()
        if cover:
            parts.append(_link("http://opds-spec.org/image", cover, "image/jpeg"))
            parts.append(_link("http://opds-spec.org/image/thumbnail", cover, "image/jpeg"))
        acq = (book.get("acquisition_href") or "").strip()
        media = book.get("media_type") or "application/epub+zip"
        if acq:
            parts.append(_link("http://opds-spec.org/acquisition", acq, media))
        parts.append("</entry>")
    parts.append("</feed>")
    return "\n".join(parts)


async def list_shelf_items(db: AsyncSession, user_id: int) -> list[UserEreaderItem]:
    result = await db.execute(
        select(UserEreaderItem)
        .where(UserEreaderItem.user_id == user_id)
        .order_by(UserEreaderItem.added_at.desc())
    )
    return list(result.scalars().all())


async def add_shelf_item(
    db: AsyncSession,
    *,
    user: User,
    series_id: int,
    chapter_id: int,
    title: str = "",
    author: str = "",
    cover_url: str = "",
) -> UserEreaderItem:
    existing = (
        await db.execute(
            select(UserEreaderItem).where(
                UserEreaderItem.user_id == user.id,
                UserEreaderItem.kavita_chapter_id == chapter_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.title = (title or existing.title or "")[:512]
        existing.author = (author or existing.author or "")[:256]
        if cover_url:
            existing.cover_url = cover_url[:1024]
        existing.kavita_series_id = series_id
        existing.added_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing)
        return existing

    row = UserEreaderItem(
        user_id=user.id,
        kavita_series_id=series_id,
        kavita_chapter_id=chapter_id,
        title=(title or "")[:512],
        author=(author or "")[:256],
        cover_url=(cover_url or "")[:1024],
        added_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def remove_shelf_item(db: AsyncSession, user_id: int, item_id: int) -> bool:
    row = (
        await db.execute(
            select(UserEreaderItem).where(
                UserEreaderItem.id == item_id,
                UserEreaderItem.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def shelf_has_chapter(db: AsyncSession, user_id: int, chapter_id: int) -> bool:
    row = (
        await db.execute(
            select(UserEreaderItem.id).where(
                UserEreaderItem.user_id == user_id,
                UserEreaderItem.kavita_chapter_id == chapter_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def library_ebook_books() -> list[dict[str, Any]]:
    """Flatten Kavita ebook series into OPDS-ready book dicts (best-effort chapter)."""
    import asyncio

    from app.services import kavita
    from app.services import kavita_ebook_match

    series_list = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
    sem = asyncio.Semaphore(10)

    async def one(series: dict) -> dict[str, Any] | None:
        sid = series.get("id")
        if sid is None:
            return None
        name = (
            series.get("name")
            or series.get("localizedName")
            or series.get("originalName")
            or "Ebook"
        )
        authors = series.get("authors") or []
        author = ""
        if authors:
            author = (
                (authors[0] or {}).get("name", "")
                if isinstance(authors[0], dict)
                else str(authors[0])
            )
        async with sem:
            volumes = await kavita.get_series_volumes(int(sid))
        book_num = kavita_ebook_match._book_number_from_text(name)
        chapter_id = kavita_ebook_match._pick_chapter_id(volumes, book_num)
        if chapter_id is None:
            for vol in volumes or []:
                chapters = vol.get("chapters") or []
                if chapters:
                    chapter_id = chapters[0].get("id")
                    break
        if chapter_id is None:
            return None
        fmt = series.get("format")
        media = "application/pdf" if fmt == kavita.PDF_FORMAT else "application/epub+zip"
        return {
            "series_id": int(sid),
            "chapter_id": int(chapter_id),
            "title": name,
            "author": author,
            "cover_path": f"/api/library/reader/cover/ebook?seriesId={sid}",
            "media_type": media,
        }

    results = await asyncio.gather(*[one(s) for s in (series_list or [])])
    return [b for b in results if b]


async def probe_kavita_opds() -> dict[str, Any]:
    """Soft-check that Kavita answers OPDS with the configured API key."""
    import httpx
    from app.services import instance_settings

    url, key, _ = await instance_settings.get_kavita_connection()
    if not url or not key:
        return {
            "configured": False,
            "ok": False,
            "error": "Kavita URL or API key not configured",
            "opdsPath": None,
        }
    opds_url = f"{url.rstrip('/')}/api/opds/{key}"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(opds_url)
            ok = resp.status_code == 200 and (
                "atom" in (resp.headers.get("content-type") or "").lower()
                or "<feed" in (resp.text or "")[:500].lower()
            )
            return {
                "configured": True,
                "ok": ok,
                "statusCode": resp.status_code,
                "error": None if ok else f"HTTP {resp.status_code}",
                "opdsPath": "/api/opds/{apiKey}",
                "note": (
                    "Kavita OPDS uses the same API key as Admin → Config → Kavita API key. "
                    "Library Site proxies a per-user feed so members never share that key."
                ),
            }
    except Exception as e:
        return {
            "configured": True,
            "ok": False,
            "error": str(e),
            "opdsPath": "/api/opds/{apiKey}",
        }
