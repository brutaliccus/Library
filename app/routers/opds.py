"""Public OPDS catalog endpoints (token in path - no JWT)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import kavita, opds as opds_svc

router = APIRouter(prefix="/api/opds", tags=["opds"])


async def _require_opds_user(token: str, db: AsyncSession):
    user = await opds_svc.user_by_opds_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid OPDS token")
    return user


def _xml(body: str) -> Response:
    return Response(
        content=body,
        media_type="application/atom+xml;profile=opds-catalog;charset=utf-8",
        headers={"Cache-Control": "private, max-age=60"},
    )


def _abs(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


@router.get("/{token}")
async def opds_root(token: str, db: AsyncSession = Depends(get_db)):
    """OPDS navigation root for one Library Site user."""
    user = await _require_opds_user(token, db)
    base = await opds_svc.public_app_base()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="App URL is not configured - set Admin Config App URL",
        )
    root = f"{base}/api/opds/{token}"
    shelf_count = len(await opds_svc.list_shelf_items(db, user.id))
    xml = opds_svc.build_navigation_feed(
        feed_id=f"urn:library-site:opds:{user.id}",
        title=f"{user.username}'s Library",
        self_href=root,
        entries=[
            {
                "id": f"urn:library-site:opds:{user.id}:shelf",
                "title": "Send to ereader",
                "summary": f"Books you sent from Library Site ({shelf_count})",
                "href": f"{root}/shelf",
                "type": opds_svc.ACQ_TYPE,
            },
            {
                "id": f"urn:library-site:opds:{user.id}:library",
                "title": "All ebooks",
                "summary": "Full ebook library (Kavita)",
                "href": f"{root}/library",
                "type": opds_svc.ACQ_TYPE,
            },
        ],
    )
    return _xml(xml)


@router.get("/{token}/shelf")
async def opds_shelf(token: str, db: AsyncSession = Depends(get_db)):
    """Acquisition feed: books the user sent to their ereader shelf."""
    user = await _require_opds_user(token, db)
    base = await opds_svc.public_app_base()
    if not base:
        raise HTTPException(status_code=503, detail="App URL is not configured")
    root = f"{base}/api/opds/{token}"
    items = await opds_svc.list_shelf_items(db, user.id)
    books = []
    for item in items:
        path = await kavita.get_chapter_file_path(item.kavita_chapter_id)
        media = opds_svc.media_type_for_path(path) if path else "application/epub+zip"
        cover = item.cover_url or f"/api/library/reader/cover/ebook?seriesId={item.kavita_series_id}"
        books.append({
            "id": f"urn:library-site:chapter:{item.kavita_chapter_id}",
            "title": item.title or f"Chapter {item.kavita_chapter_id}",
            "author": item.author,
            "updated": item.added_at,
            "cover_href": _abs(base, cover),
            "acquisition_href": f"{root}/download/{item.kavita_chapter_id}",
            "media_type": media,
        })
    xml = opds_svc.build_acquisition_feed(
        feed_id=f"urn:library-site:opds:{user.id}:shelf",
        title="Send to ereader",
        self_href=f"{root}/shelf",
        root_href=root,
        books=books,
    )
    return _xml(xml)


@router.get("/{token}/library")
async def opds_library(token: str, db: AsyncSession = Depends(get_db)):
    """Acquisition feed: all Kavita ebooks (token grants library access)."""
    user = await _require_opds_user(token, db)
    base = await opds_svc.public_app_base()
    if not base:
        raise HTTPException(status_code=503, detail="App URL is not configured")
    root = f"{base}/api/opds/{token}"
    raw = await opds_svc.library_ebook_books()
    books = []
    for b in raw:
        cid = b["chapter_id"]
        media = b.get("media_type") or "application/epub+zip"
        books.append({
            "id": f"urn:library-site:chapter:{cid}",
            "title": b.get("title") or "Ebook",
            "author": b.get("author") or "",
            "cover_href": _abs(base, b.get("cover_path") or ""),
            "acquisition_href": f"{root}/download/{cid}",
            "media_type": media,
        })
    xml = opds_svc.build_acquisition_feed(
        feed_id=f"urn:library-site:opds:{user.id}:library",
        title="All ebooks",
        self_href=f"{root}/library",
        root_href=root,
        books=books,
    )
    return _xml(xml)


@router.get("/{token}/download/{chapter_id}")
async def opds_download(
    token: str,
    chapter_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Download an ebook file authenticated by OPDS token."""
    await _require_opds_user(token, db)
    path = await kavita.get_chapter_file_path(chapter_id)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Book file not found")
    media_type = opds_svc.media_type_for_path(path)
    filename = path.name
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Accept-Ranges": "bytes",
        },
    )
