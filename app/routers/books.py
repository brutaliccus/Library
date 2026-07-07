from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, SearchHistory
from app.utils.auth import get_current_user
import re

from app.services import google_books
from app.services import goodreads
from app.services import indexer_cache
from app.services import catalog_match
from app.services.download_discovery import resolve_book_search_context
from app.utils.book_series import detect_series_from_title as _detect_series_from_title
from app.utils.book_series import series_name_match as _series_name_match

router = APIRouter(prefix="/api/books", tags=["books"])


async def _apply_availability_filter(books: list, available_only: bool) -> list:
    if not available_only or not books:
        return books

    volume_ids = [b.get("volumeId") or b.get("id") for b in books if b.get("volumeId") or b.get("id")]
    avail = await indexer_cache.volume_ids_with_matches(volume_ids)

    filtered = []
    for b in books:
        vid = b.get("volumeId") or b.get("id")
        if not vid:
            continue
        info = avail.get(vid)
        if not info:
            title = b.get("title", "")
            authors = b.get("authors") or []
            author = authors[0] if authors else ""
            ctx = resolve_book_search_context(
                title=title,
                author=author,
                subtitle=b.get("subtitle", ""),
            )
            cache_hits = await indexer_cache.get_torrents_for_book(
                ctx, tiers=("exact", "likely"), max_results=5,
            )
            if cache_hits:
                info = {
                    "available": True,
                    "matchCount": len(cache_hits),
                    "instantRd": any(h.get("rdCached") for h in cache_hits),
                    "instantTorbox": any(h.get("torboxCached") for h in cache_hits),
                }
                isbns = [x for x in (b.get("isbn13"), b.get("isbn10")) if x]
                await catalog_match.match_volume_to_torrents(vid, title, author, isbns=isbns or None)
        if info and info.get("available"):
            row = dict(b)
            row["availability"] = info
            filtered.append(row)
    return filtered


def _is_browse_query(q: str) -> bool:
    """Genre/category shelf queries (not free-text title search)."""
    low = q.strip().lower()
    return (
        low == "subject:fiction"
        or low.startswith("__genre__:")
        or low in ("__popular__", "__new__")
    )


@router.get("/available")
async def list_available_books(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=40),
    _user: User = Depends(get_current_user),
):
    """Cache-first browse: Google Books cards for volumes with indexer matches."""
    from sqlalchemy import func, desc
    from app.database import async_session
    from app.models import CatalogTorrentMatch, IndexerTorrent

    start = (page - 1) * pageSize
    async with async_session() as db:
        match_filter = (
            CatalogTorrentMatch.match_tier.in_(("exact", "likely")),
            IndexerTorrent.is_active.is_(True),
        )
        total = await db.scalar(
            select(func.count(func.distinct(CatalogTorrentMatch.google_volume_id)))
            .select_from(CatalogTorrentMatch)
            .join(IndexerTorrent, CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash)
            .where(*match_filter)
        ) or 0
        rows = (
            await db.execute(
                select(
                    CatalogTorrentMatch.google_volume_id,
                    func.max(CatalogTorrentMatch.score).label("best"),
                )
                .join(IndexerTorrent, CatalogTorrentMatch.info_hash == IndexerTorrent.info_hash)
                .where(*match_filter)
                .group_by(CatalogTorrentMatch.google_volume_id)
                .order_by(desc("best"))
                .offset(start)
                .limit(pageSize)
            )
        ).all()

    books = []
    for row in rows:
        vid = row[0]
        detail = await google_books.get_volume(vid)
        if not detail:
            continue
        info = await indexer_cache.get_volume_availability(vid)
        books.append({**detail, "availability": info})

    return {
        "books": books,
        "totalItems": total,
        "page": page,
        "pageSize": pageSize,
        "availableOnly": True,
        "source": "cache",
    }


@router.get("/search")
async def search_books(
    q: str = Query(..., min_length=1, description="Search query"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=40),
    available_only: bool = Query(True, description="Only show books with cached indexer matches"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    start_index = (page - 1) * pageSize

    order = "relevance"
    if q.strip().lower() == "__available__":
        result = await list_available_books(page=page, pageSize=pageSize, _user=user)
        return result
    elif q.strip().lower() == "__popular__":
        q = "subject:fiction"
        order = "relevance"
    elif q.strip().lower() == "__new__":
        q = "subject:fiction"
        order = "newest"
    elif q.strip().lower().startswith("__genre__:"):
        slug = q.strip()[10:]
        result = await google_books.search_by_genre(
            slug, max_results=pageSize, start_index=start_index, order_by=order,
            multi_query=True,
        )
        books = await _apply_availability_filter(result["books"], available_only)
        if available_only and not books and _is_browse_query(q):
            books = result["books"]
        return {
            "books": books,
            "totalItems": result["totalItems"] if not available_only else max(len(books), result["totalItems"]),
            "page": page,
            "pageSize": pageSize,
            "availableOnly": available_only,
        }
    elif "+" in q and q.strip().lower().startswith("subject:"):
        parts = [p.strip() for p in q.split("+") if p.strip()]
        q = " ".join(parts)

    result = await google_books.search_volumes(
        q, max_results=pageSize, start_index=start_index, order_by=order,
    )

    books = await _apply_availability_filter(result["books"], available_only)
    if available_only and not books and _is_browse_query(q):
        books = result["books"]

    clean_q = q.strip()
    if not clean_q.lower().startswith(("subject:", "__")):
        db.add(SearchHistory(user_id=user.id, query=clean_q))
        await db.commit()

    return {
        "books": books,
        "totalItems": result["totalItems"] if not available_only else len(books),
        "page": page,
        "pageSize": pageSize,
        "availableOnly": available_only,
    }


@router.get("/trending")
async def trending_books(
    _user: User = Depends(get_current_user),
):
    books = await google_books.get_trending(max_results=20)
    return {"books": books}


@router.get("/new-releases")
async def new_releases(
    _user: User = Depends(get_current_user),
):
    books = await google_books.get_new_releases(max_results=20)
    return {"books": books}


@router.get("/categories")
async def list_categories(
    _user: User = Depends(get_current_user),
):
    cats = [
        {"slug": slug, "name": name}
        for slug, name in google_books.CATEGORY_SLUGS.items()
    ]
    return {"categories": cats}


@router.get("/genres")
async def list_genres(
    _user: User = Depends(get_current_user),
):
    """Return the full hierarchical genre taxonomy."""
    taxonomy = []
    for g in google_books.GENRE_TAXONOMY:
        taxonomy.append({
            "slug": g["slug"],
            "name": g["name"],
            "icon": g.get("icon", ""),
            "children": [
                {"slug": c["slug"], "name": c["name"]}
                for c in g.get("children", [])
            ],
        })
    return {"genres": taxonomy}


@router.get("/category/{slug}")
async def books_by_category(
    slug: str,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=40),
    _user: User = Depends(get_current_user),
):
    start_index = (page - 1) * pageSize

    if slug == "all":
        result = await google_books.search_volumes(
            "subject:fiction", max_results=pageSize, start_index=start_index,
        )
        name = "All"
    elif slug == "popular":
        result = await google_books.search_volumes(
            "subject:fiction", max_results=pageSize, start_index=start_index, order_by="relevance",
        )
        name = "Popular"
    elif slug == "new":
        result = await google_books.search_volumes(
            "subject:fiction", max_results=pageSize, start_index=start_index, order_by="newest",
        )
        name = "New Releases"
    else:
        genre = google_books.get_genre_info(slug)
        if genre:
            result = await google_books.search_by_genre(
                slug, max_results=pageSize, start_index=start_index,
                multi_query=False,
            )
            name = genre.get("name", slug)
        else:
            name = google_books.CATEGORY_SLUGS.get(slug)
            if not name:
                raise HTTPException(status_code=404, detail="Category not found")
            result = await google_books.get_by_category(
                name, max_results=pageSize, start_index=start_index,
            )

    return {
        "books": result["books"],
        "totalItems": result["totalItems"],
        "category": name,
        "page": page,
        "pageSize": pageSize,
    }


@router.get("/recent-searches")
async def recent_searches(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(SearchHistory.query, func.max(SearchHistory.created_at).label("latest"))
        .where(SearchHistory.user_id == user.id)
        .group_by(SearchHistory.query)
        .order_by(desc("latest"))
        .limit(10)
    )
    rows = (await db.execute(stmt)).all()
    return {"searches": [row[0] for row in rows]}


@router.get("/popular-searches")
async def popular_searches(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(SearchHistory.query, func.count().label("cnt"))
        .where(SearchHistory.created_at >= cutoff)
        .group_by(SearchHistory.query)
        .order_by(desc("cnt"))
        .limit(10)
    )
    rows = (await db.execute(stmt)).all()
    return {"searches": [row[0] for row in rows]}


@router.get("/availability/{volume_id:path}")
async def book_availability(
    volume_id: str,
    _user: User = Depends(get_current_user),
):
    """Lightweight cached torrent availability for a catalog volume."""
    info = await indexer_cache.get_volume_availability(volume_id)
    return {"volumeId": volume_id, **info}


@router.get("/rating/{volume_id:path}")
async def get_goodreads_rating(
    volume_id: str,
    _user: User = Depends(get_current_user),
):
    """Fetch Goodreads rating for a book (by ISBN or title lookup)."""
    if volume_id.startswith("OL:"):
        ol_key = volume_id[3:]
        book = await google_books.get_open_library_work(ol_key)
    else:
        book = await google_books.get_volume(volume_id)
    if not book:
        return {"goodreadsRating": 0, "goodreadsCount": 0, "goodreadsReviewCount": 0}

    result = await goodreads.get_rating(
        isbn13=book.get("isbn13", ""),
        isbn10=book.get("isbn10", ""),
        title=book.get("title", ""),
        author=(book.get("authors", []) or [""])[0],
    )
    return result or {"goodreadsRating": 0, "goodreadsCount": 0, "goodreadsReviewCount": 0}


@router.get("/series/{volume_id:path}")
async def get_book_series(
    volume_id: str,
    _user: User = Depends(get_current_user),
):
    """Detect series for a book and return all books in the series."""
    if volume_id.startswith("OL:"):
        ol_key = volume_id[3:]
        book = await google_books.get_open_library_work(ol_key)
    else:
        book = await google_books.get_volume(volume_id)
    if not book:
        return {"seriesName": None, "books": [], "currentBookIndex": -1}

    title = book.get("title", "")
    subtitle = book.get("subtitle", "")
    authors = book.get("authors", [])
    author = authors[0] if authors else ""
    full_title = f"{title}: {subtitle}" if subtitle else title

    series_name = None
    sequence = None

    gbooks_series = book.get("seriesName", "")
    gbooks_seq = book.get("seriesBookNumber", "")
    if gbooks_series:
        series_name = gbooks_series
        sequence = str(gbooks_seq) if gbooks_seq else None

    if not series_name:
        detected = _detect_series_from_title(full_title) or _detect_series_from_title(title)
        if detected:
            series_name, sequence = detected

    if not series_name and subtitle:
        detected = _detect_series_from_title(subtitle)
        if detected:
            series_name, sequence = detected[0], detected[1]

    if not series_name:
        return {"seriesName": None, "books": [], "currentBookIndex": -1}

    search_q = f'intitle:"{series_name}" inauthor:{author}' if author else f'intitle:"{series_name}"'
    result = await google_books.search_volumes(search_q, max_results=40)
    raw_books = result.get("books", [])

    seen_ids = set()
    series_books = []
    for b in raw_books:
        if b["id"] in seen_ids:
            continue
        seen_ids.add(b["id"])
        b_title = b.get("title", "")
        b_subtitle = b.get("subtitle", "")
        b_full = f"{b_title}: {b_subtitle}" if b_subtitle else b_title

        det = _detect_series_from_title(b_full) or _detect_series_from_title(b_title)
        seq = ""

        if det and _series_name_match(det[0], series_name):
            seq = det[1]
        elif series_name.lower() in b_title.lower() or series_name.lower() in b_full.lower():
            det2 = _detect_series_from_title(b_subtitle) if b_subtitle else None
            seq = det2[1] if det2 else ""
        else:
            continue

        series_books.append({
            "id": b["id"],
            "title": b_title,
            "subtitle": b_subtitle,
            "coverUrl": b.get("coverUrl", ""),
            "authors": b.get("authors", []),
            "sequence": seq,
            "publishedDate": b.get("publishedDate", ""),
        })

    if volume_id not in seen_ids and not volume_id.startswith("OL:"):
        series_books.insert(0, {
            "id": volume_id,
            "title": title,
            "subtitle": subtitle,
            "coverUrl": book.get("coverUrl", ""),
            "authors": authors,
            "sequence": sequence or "",
            "publishedDate": book.get("publishedDate", ""),
        })

    try:
        series_books.sort(key=lambda x: float(x["sequence"]) if x["sequence"] else 999)
    except (ValueError, TypeError):
        series_books.sort(key=lambda x: x.get("sequence", ""))

    current_idx = -1
    for i, b in enumerate(series_books):
        if b["id"] == volume_id:
            current_idx = i
            break

    if len(series_books) < 2:
        return {"seriesName": None, "books": [], "currentBookIndex": -1}

    return {
        "seriesName": series_name,
        "books": series_books,
        "currentBookIndex": current_idx,
    }


@router.get("/{volume_id:path}")
async def get_book_detail(
    volume_id: str,
    _user: User = Depends(get_current_user),
):
    if volume_id.startswith("OL:"):
        ol_key = volume_id[3:]
        book = await google_books.get_open_library_work(ol_key)
    else:
        book = await google_books.get_volume(volume_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book
