import asyncio
import logging
import re
import time as _time

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User, SearchHistory, DownloadRequest
from app.utils.auth import get_current_user
from app.database import async_session
from app.services import google_books
from app.services import goodreads
from app.services import hardcover
from app.services import indexer_cache
from app.services import nyt_books
from app.services import ol_catalog
from app.services import availability_alerts
from app.services import shelf_snapshots
from app.services import audiobookshelf, kavita
from app.utils.book_series import detect_series_from_title as _detect_series_from_title
from app.utils.book_series import series_name_match as _series_name_match

router = APIRouter(prefix="/api/books", tags=["books"])
logger = logging.getLogger(__name__)
settings = get_settings()

# --- Small in-memory TTL cache for slow-changing catalog shelves --------------
# Backed by persistent daily snapshots for trending / new-releases so a quiet
# night + restart still serves yesterday's shelves until today's rebuild.
_shelf_cache: dict[str, tuple[float, Any]] = {}
_SHELF_TTL = 1800.0  # 30 min hot cache


def _shelf_get(key: str):
    hit = _shelf_cache.get(key)
    if hit and (_time.monotonic() - hit[0]) < _SHELF_TTL:
        return hit[1]
    return None


def _shelf_put(key: str, value: Any) -> None:
    _shelf_cache[key] = (_time.monotonic(), value)


def _with_refreshed_at(payload: dict, refreshed_at: str | None = None) -> dict:
    out = dict(payload)
    out["refreshedAt"] = refreshed_at or datetime.now(timezone.utc).isoformat()
    return out


async def _load_daily_shelf(name: str) -> dict | None:
    """Memory → same-UTC-day snapshot → None (caller rebuilds)."""
    cached = _shelf_get(name)
    if cached is not None:
        return cached
    snap = await shelf_snapshots.get_snapshot(name)
    if snap and shelf_snapshots.same_utc_day(snap):
        payload = snap.get("payload")
        if isinstance(payload, dict) and payload.get("books") is not None:
            payload = _with_refreshed_at(payload, snap.get("refreshedAt"))
            _shelf_put(name, payload)
            return payload
    return None


async def _store_daily_shelf(name: str, payload: dict) -> dict:
    payload = _with_refreshed_at(payload)
    _shelf_put(name, payload)
    try:
        await shelf_snapshots.put_snapshot(name, payload)
    except Exception as e:
        logger.warning("Failed to persist shelf snapshot %s: %s", name, e)
    return payload


async def _catalog_search_volumes(q: str, *, max_results: int, start_index: int, order_by: str) -> dict:
    timeout = float(getattr(settings, "open_library_search_timeout", 12.0))
    try:
        return await asyncio.wait_for(
            google_books.search_volumes(
                q, max_results=max_results, start_index=start_index, order_by=order_by,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("catalog search timed out after %.0fs for %r", timeout, q[:60])
        return {"books": [], "totalItems": 0}


_UNAVAILABLE = {
    "available": False,
    "matchCount": 0,
    "instantRd": False,
    "instantTorbox": False,
    "inLibrary": False,
    "catalogOnly": True,
}

_home_shelves_lock = asyncio.Lock()


def _norm_title_key(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _title_in_holdings(title: str, holdings: set[str]) -> bool:
    key = _norm_title_key(title)
    if not key or not holdings:
        return False
    if key in holdings:
        return True
    if len(key) < 10:
        return False
    for h in holdings:
        if len(h) >= 10 and (key in h or h in key):
            return True
    return False


async def _library_holding_title_keys() -> set[str]:
    """Normalized titles currently in ABS / Kavita / non-failed download requests.

    ABS/Kavita lists are process-cached (~5 min) so this stays cheap on shelves.
    Private downloads still count — badges prevent duplicate requests without
    revealing who requested the book.
    """
    keys: set[str] = set()
    try:
        for item in await audiobookshelf.get_all_items():
            k = _norm_title_key(item.get("title") or "")
            if k:
                keys.add(k)
    except Exception:
        logger.debug("holdings: ABS unavailable", exc_info=True)
    try:
        for s in await kavita.get_all_series(formats=kavita.EBOOK_FORMATS):
            name = s.get("name") or s.get("localizedName") or s.get("originalName") or ""
            k = _norm_title_key(name)
            if k:
                keys.add(k)
    except Exception:
        logger.debug("holdings: Kavita unavailable", exc_info=True)
    try:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(DownloadRequest.title).where(DownloadRequest.status != "failed")
                )
            ).scalars().all()
            for t in rows:
                k = _norm_title_key(t or "")
                if k:
                    keys.add(k)
    except Exception:
        logger.debug("holdings: download_requests unavailable", exc_info=True)
    return keys


async def _annotate_availability(books: list) -> list:
    """Batch-attach indexer cache badges + in-library flags for store tiles.

    In-library uses cached ABS/Kavita holdings (not a fresh full pull each time).
    """
    if not books:
        return books

    volume_ids = [b.get("volumeId") or b.get("id") for b in books if b.get("volumeId") or b.get("id")]
    avail_task = asyncio.create_task(indexer_cache.volume_ids_with_matches(volume_ids))
    holdings_task = asyncio.create_task(_library_holding_title_keys())
    avail, holdings = await asyncio.gather(avail_task, holdings_task)

    annotated = []
    for b in books:
        vid = b.get("volumeId") or b.get("id")
        row = dict(b)
        base = dict(avail.get(vid, _UNAVAILABLE) if vid else _UNAVAILABLE)
        available = bool(base.get("available"))
        in_library = _title_in_holdings(b.get("title") or "", holdings)
        row["availability"] = {
            "available": available or in_library,
            "matchCount": int(base.get("matchCount") or 0),
            "instantRd": bool(base.get("instantRd")),
            "instantTorbox": bool(base.get("instantTorbox")),
            "inLibrary": in_library,
            # Yellow "?" — catalog hit with no cached torrent match and not on disk.
            "catalogOnly": not available and not in_library,
        }
        annotated.append(row)
    return annotated


async def _apply_availability_filter(books: list, available_only: bool) -> list:
    annotated = await _annotate_availability(books)
    if not available_only:
        return annotated
    return [b for b in annotated if b.get("availability", {}).get("available")]


async def _fetch_volume_cards(volume_ids: list[str]) -> list[dict]:
    if not volume_ids:
        return []

    sem = asyncio.Semaphore(6)

    async def _one(vid: str) -> dict | None:
        async with sem:
            try:
                detail = await asyncio.wait_for(
                    google_books.get_catalog_volume(vid),
                    timeout=12.0,
                )
            except asyncio.TimeoutError:
                logger.debug("catalog volume timed out: %s", vid)
                return None
            if not detail:
                return None
            return detail

    results = await asyncio.gather(*(_one(vid) for vid in volume_ids), return_exceptions=True)
    books: list[dict] = []
    for item in results:
        if isinstance(item, dict):
            books.append(item)
    return await _annotate_availability(books)


def _norm_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 3}


async def _match_title_to_available_id(
    title: str, author: str, avail_ids: set[str],
) -> str | None:
    """Resolve a real-world (title, author) to a local OL volume id we HAVE.

    Searches the local catalog by title, then picks the candidate that (a) is in
    our available set and (b) shares an author token (or is an exact title match
    when the source author is unknown). Returns the OL volume id or None.
    """
    if not title:
        return None
    try:
        candidates = await ol_catalog.search_by_title(title, limit=6)
    except Exception:
        return None
    if not candidates:
        return None

    author_tokens = _norm_tokens(author)
    title_norm = " ".join(sorted(_norm_tokens(title)))

    fallback: str | None = None
    for c in candidates:
        vid = c.get("volumeId") or c.get("id")
        if not vid or vid not in avail_ids:
            continue
        cand_authors = " ".join(c.get("authors") or [])
        cand_author_tokens = _norm_tokens(cand_authors)
        if author_tokens and (author_tokens & cand_author_tokens):
            return vid  # confident: author overlap
        # Otherwise remember the first available exact-title match as a fallback.
        if fallback is None and " ".join(sorted(_norm_tokens(c.get("title", "")))) == title_norm:
            fallback = vid
    return fallback


async def _nyt_trending_available_cards(limit: int = 20) -> list[dict]:
    """Real NYT bestsellers, filtered to the ones we can actually download."""
    titles = await nyt_books.get_trending_titles(max_results=60)
    if not titles:
        return []

    # Gather candidate OL ids by title, then one batched availability check.
    per_title_candidates: list[tuple[dict, list[str]]] = []
    all_candidate_ids: list[str] = []
    for entry in titles:
        try:
            cands = await ol_catalog.search_by_title(entry["title"], limit=6)
        except Exception:
            cands = []
        ids = [c.get("volumeId") or c.get("id") for c in cands if (c.get("volumeId") or c.get("id"))]
        per_title_candidates.append((entry, ids))
        all_candidate_ids.extend(ids)

    if not all_candidate_ids:
        return []
    avail = await indexer_cache.volume_ids_with_matches(all_candidate_ids)
    avail_ids = {vid for vid, info in avail.items() if info.get("available")}
    if not avail_ids:
        return []

    ordered_ids: list[str] = []
    seen: set[str] = set()
    for entry, _ids in per_title_candidates:
        vid = await _match_title_to_available_id(entry["title"], entry.get("author", ""), avail_ids)
        if vid and vid not in seen:
            seen.add(vid)
            ordered_ids.append(vid)
        if len(ordered_ids) >= limit:
            break
    return await _fetch_volume_cards(ordered_ids)


async def _genre_available_cards(
    slug: str, *, page: int, page_size: int, order_by: str = "score",
) -> dict | None:
    """Genre browse starting from volumes we actually have torrents for.

    Filters the matched-volume subject index by the genre's subjects, so a broad
    genre returns hundreds of available titles (ordered by match score) instead
    of the tiny overlap the OL-subject-first scan produced. Returns None when the
    subject index has no hits (caller falls back to the catalog scan).
    """
    match_expr = google_books.genre_subject_fts_expr(slug)
    if not match_expr:
        return None
    volume_ids, total = await indexer_cache.list_matched_volume_ids_by_subject(
        match_expr, page=page, page_size=page_size, order_by=order_by, need_total=True,
    )
    if not volume_ids:
        return None
    books = await _fetch_volume_cards(volume_ids)
    return {"books": books, "totalItems": total}


async def _paginate_catalog_available(
    fetch_page,
    *,
    page: int,
    page_size: int,
    max_scan_pages: int = 8,
) -> dict:
    """Scan forward through catalog results until we can fill an available-only page."""
    need_end = page * page_size
    available_books: list[dict] = []
    google_start = 0
    google_total = 0
    scanned = 0

    while len(available_books) < need_end and scanned < max_scan_pages:
        chunk = await fetch_page(google_start)
        google_total = int(chunk.get("totalItems") or 0)
        raw = chunk.get("books") or []
        if not raw:
            break
        available_books.extend(await _apply_availability_filter(raw, True))
        google_start += len(raw)
        scanned += 1
        if len(raw) < page_size:
            break

    start = (page - 1) * page_size
    return {
        "books": available_books[start : start + page_size],
        "totalItems": len(available_books),
        "googleTotalItems": google_total,
    }


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
    order_by: str = Query("score", description="score | recent"),
    _user: User = Depends(get_current_user),
):
    """Cache-first browse: Google Books cards for volumes with indexer matches."""
    sort = "recent" if order_by.strip().lower() == "recent" else "score"
    volume_ids, total = await indexer_cache.list_matched_volume_ids(
        page=page, page_size=pageSize, order_by=sort,
    )
    books = await _fetch_volume_cards(volume_ids)

    return {
        "books": books,
        "totalItems": total,
        "page": page,
        "pageSize": pageSize,
        "availableOnly": True,
        "source": "cache",
        "orderBy": sort,
    }


@router.get("/search")
async def search_books(
    q: str = Query(..., min_length=1, description="Search query"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=40),
    available_only: bool = Query(False, description="Only show books with cached indexer matches"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    start_index = (page - 1) * pageSize

    order = "relevance"
    if q.strip().lower() == "__available__":
        result = await list_available_books(page=page, pageSize=pageSize, _user=user)
        return result
    elif q.strip().lower() == "__popular__":
        if available_only:
            return await list_available_books(page=page, pageSize=pageSize, order_by="score", _user=user)
        q = "subject:fiction"
        order = "relevance"
    elif q.strip().lower() == "__new__":
        if available_only:
            return await list_available_books(page=page, pageSize=pageSize, order_by="recent", _user=user)
        q = "subject:fiction"
        order = "newest"
    elif q.strip().lower().startswith("__genre__:"):
        slug = q.strip()[10:]
        # Available-only genre browse: start from volumes we HAVE torrents for and
        # filter by subject (returns hundreds), not the tiny OL-subject overlap.
        if available_only:
            try:
                fast = await _genre_available_cards(
                    slug, page=page, page_size=pageSize, order_by="score",
                )
            except Exception as e:
                logger.warning("genre subject browse failed for %s: %s", slug, e)
                fast = None
            if fast is not None:
                return {
                    "books": fast["books"],
                    "totalItems": fast["totalItems"],
                    "page": page,
                    "pageSize": pageSize,
                    "availableOnly": True,
                }
        try:
            result = await google_books.search_by_genre(
                slug, max_results=pageSize, start_index=start_index, order_by=order,
                multi_query=True,
            )
        except Exception as e:
            logger.warning("genre search failed for %s: %s", slug, e)
            return {
                "books": [],
                "totalItems": 0,
                "page": page,
                "pageSize": pageSize,
                "availableOnly": available_only,
            }
        books = await _apply_availability_filter(result["books"], available_only)
        total_items = result["totalItems"] if not available_only else len(books)
        if available_only and not books:
            filled = await _paginate_catalog_available(
                lambda start: google_books.search_by_genre(
                    slug, max_results=pageSize, start_index=start, order_by=order, multi_query=True,
                ),
                page=page,
                page_size=pageSize,
            )
            books = filled["books"]
            total_items = filled["totalItems"]
        return {
            "books": books,
            "totalItems": total_items,
            "page": page,
            "pageSize": pageSize,
            "availableOnly": available_only,
        }
    elif "+" in q and q.strip().lower().startswith("subject:"):
        parts = [p.strip() for p in q.split("+") if p.strip()]
        q = " ".join(parts)

    try:
        result = await _catalog_search_volumes(
            q, max_results=pageSize, start_index=start_index, order_by=order,
        )
    except Exception as e:
        logger.warning("book search failed for %r: %s", q, e)
        return {
            "books": [],
            "totalItems": 0,
            "page": page,
            "pageSize": pageSize,
            "availableOnly": available_only,
        }

    books = await _apply_availability_filter(result["books"], available_only)
    if available_only and not books and _is_browse_query(q):
        filled = await _paginate_catalog_available(
            lambda start: _catalog_search_volumes(
                q, max_results=pageSize, start_index=start, order_by=order,
            ),
            page=page,
            page_size=pageSize,
        )
        books = filled["books"]
        total_items = filled["totalItems"]
    else:
        total_items = result["totalItems"] if not available_only else len(books)

    clean_q = q.strip()
    if not clean_q.lower().startswith(("subject:", "__")):
        try:
            db.add(SearchHistory(user_id=user.id, query=clean_q))
            await db.commit()
        except Exception as e:
            logger.warning("search history write failed: %s", e)
            await db.rollback()

    if "total_items" not in locals():
        total_items = result["totalItems"] if not available_only else len(books)

    return {
        "books": books,
        "totalItems": total_items,
        "page": page,
        "pageSize": pageSize,
        "availableOnly": available_only,
    }


async def build_trending_payload() -> dict:
    books = await _nyt_trending_available_cards(limit=20)
    source = "nyt"
    if not books:
        volume_ids, _ = await indexer_cache.list_matched_volume_ids(
            page=1, page_size=20, order_by="score", need_total=False,
        )
        books = await _fetch_volume_cards(volume_ids)
        source = "cache"
    return {"books": books, "source": source, "availableOnly": True}


async def build_new_releases_payload() -> dict:
    volume_ids, _ = await indexer_cache.list_matched_volumes_by_year(
        page=1, page_size=20, min_year=1,
    )
    source = "pubdate"
    if not volume_ids:
        volume_ids, _ = await indexer_cache.list_matched_volume_ids(
            page=1, page_size=20, order_by="recent", need_total=False,
        )
        source = "cache"
    books = await _fetch_volume_cards(volume_ids)
    return {"books": books, "source": source, "availableOnly": True}


async def refresh_daily_shelves(*, force: bool = False) -> dict[str, bool]:
    """Rebuild trending + new-releases when stale (or force). Used by startup cron."""
    results = {"trending": False, "new-releases": False}
    for name, builder in (
        ("trending", build_trending_payload),
        ("new-releases", build_new_releases_payload),
    ):
        try:
            if not force:
                snap = await shelf_snapshots.get_snapshot(name)
                if shelf_snapshots.same_utc_day(snap):
                    continue
            elif name in _shelf_cache:
                # Drop same-day memory snapshot so visitors don't keep seeing
                # blank OL stub covers while the forced rebuild runs.
                _shelf_cache.pop(name, None)
            payload = await builder()
            if payload.get("books"):
                await _store_daily_shelf(name, payload)
                results[name] = True
                logger.info("Daily shelf refreshed: %s (%s books)", name, len(payload["books"]))
        except Exception as e:
            logger.warning("Daily shelf refresh failed for %s: %s", name, e)
    return results


@router.get("/trending")
async def trending_books(
    _user: User = Depends(get_current_user),
):
    cached = await _load_daily_shelf("trending")
    if cached is not None:
        return cached
    # Stale snapshot OK while we rebuild — prefer any persisted books over empty.
    stale = await shelf_snapshots.get_snapshot("trending")
    try:
        payload = await build_trending_payload()
        if payload.get("books"):
            return await _store_daily_shelf("trending", payload)
        if stale and isinstance(stale.get("payload"), dict):
            return _with_refreshed_at(stale["payload"], stale.get("refreshedAt"))
        return payload
    except Exception as e:
        logger.warning("trending books failed: %s", e)
        if stale and isinstance(stale.get("payload"), dict):
            return _with_refreshed_at(stale["payload"], stale.get("refreshedAt"))
        return {"books": [], "source": "cache", "availableOnly": True}


@router.get("/new-releases")
async def new_releases(
    _user: User = Depends(get_current_user),
):
    cached = await _load_daily_shelf("new-releases")
    if cached is not None:
        return cached
    stale = await shelf_snapshots.get_snapshot("new-releases")
    try:
        payload = await build_new_releases_payload()
        if payload.get("books"):
            return await _store_daily_shelf("new-releases", payload)
        if stale and isinstance(stale.get("payload"), dict):
            return _with_refreshed_at(stale["payload"], stale.get("refreshedAt"))
        return payload
    except Exception as e:
        logger.warning("new-releases failed: %s", e)
        if stale and isinstance(stale.get("payload"), dict):
            return _with_refreshed_at(stale["payload"], stale.get("refreshedAt"))
        return {"books": [], "source": "cache", "availableOnly": True}


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
    available_only: bool = Query(True, description="Only show books with cached indexer matches"),
    _user: User = Depends(get_current_user),
):
    start_index = (page - 1) * pageSize

    # Cache the common carousel case (first page, available-only) — identical for
    # every user and hit by all 6 Home genre shelves on each cold load.
    cache_key = f"cat:{slug}:{page}:{pageSize}" if (available_only and page == 1) else None
    if cache_key:
        cached = _shelf_get(cache_key)
        if cached is not None:
            return cached

    try:
        if slug in ("all", "available") and available_only:
            result = await list_available_books(page=page, pageSize=pageSize, _user=_user)
            return {
                **result,
                "category": "All Books" if slug == "all" else "Available to Download",
            }
        if slug == "popular":
            if available_only:
                result = await list_available_books(
                    page=page, pageSize=pageSize, order_by="score", _user=_user,
                )
                return {**result, "category": "Popular"}
            result = await google_books.search_volumes(
                "fiction", max_results=pageSize, start_index=start_index,
            )
            name = "Popular"
        elif slug == "new":
            if available_only:
                result = await list_available_books(
                    page=page, pageSize=pageSize, order_by="recent", _user=_user,
                )
                return {**result, "category": "New Releases"}
            result = await google_books.search_volumes(
                "fiction", max_results=pageSize, start_index=start_index, order_by="newest",
            )
            name = "New Releases"
        elif slug == "all":
            result = await google_books.search_volumes(
                "fiction", max_results=pageSize, start_index=start_index,
            )
            name = "All"
        else:
            genre = google_books.get_genre_info(slug)
            if genre:
                name = genre.get("name", slug)

                async def fetch_genre(start: int) -> dict:
                    return await google_books.search_by_genre(
                        slug, max_results=pageSize, start_index=start, multi_query=False,
                    )

                if available_only:
                    # Fast path: browse from volumes we have, filtered by subject.
                    try:
                        fast = await _genre_available_cards(
                            slug, page=page, page_size=pageSize, order_by="score",
                        )
                    except Exception as e:
                        logger.warning("genre subject browse failed for %s: %s", slug, e)
                        fast = None
                    if fast is not None:
                        payload = {
                            "books": fast["books"],
                            "totalItems": fast["totalItems"],
                            "category": name,
                            "page": page,
                            "pageSize": pageSize,
                            "availableOnly": True,
                            "source": "cache-subject",
                        }
                        if cache_key and fast["books"]:
                            _shelf_put(cache_key, payload)
                        return payload
                    filled = await _paginate_catalog_available(
                        fetch_genre, page=page, page_size=pageSize,
                    )
                    payload = {
                        "books": filled["books"],
                        "totalItems": filled["totalItems"],
                        "category": name,
                        "page": page,
                        "pageSize": pageSize,
                        "availableOnly": True,
                        "source": "cache-filtered",
                    }
                    if cache_key and filled["books"]:
                        _shelf_put(cache_key, payload)
                    return payload
                result = await fetch_genre(start_index)
            else:
                name = google_books.CATEGORY_SLUGS.get(slug)
                if not name:
                    raise HTTPException(status_code=404, detail="Category not found")
                result = await google_books.get_by_category(
                    name, max_results=pageSize, start_index=start_index,
                )

        books = await _apply_availability_filter(result["books"], available_only)
        return {
            "books": books,
            "totalItems": result["totalItems"] if not available_only else len(books),
            "category": name,
            "page": page,
            "pageSize": pageSize,
            "availableOnly": available_only,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("category %s failed: %s", slug, e)
        return {
            "books": [],
            "totalItems": 0,
            "category": slug,
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


class AvailabilityAlertBody(BaseModel):
    volumeId: str = Field(..., min_length=1, max_length=64)
    title: str = ""
    author: str = ""
    coverUrl: str = ""


@router.get("/availability-alerts")
async def list_availability_alerts(
    user: User = Depends(get_current_user),
):
    rows = await availability_alerts.list_alerts(user.id)
    return {
        "alerts": [
            {
                "volumeId": r.google_volume_id,
                "title": r.title,
                "author": r.author,
                "coverUrl": r.cover_url,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/availability-alerts/{volume_id:path}")
async def get_availability_alert(
    volume_id: str,
    user: User = Depends(get_current_user),
):
    row = await availability_alerts.get_alert(user.id, volume_id)
    return {"watching": row is not None}


@router.post("/availability-alerts")
async def create_availability_alert(
    body: AvailabilityAlertBody,
    user: User = Depends(get_current_user),
):
    info = await indexer_cache.get_volume_availability(body.volumeId)
    if info.get("available"):
        return {
            "watching": False,
            "alreadyAvailable": True,
            "message": "This book is already in the download cache",
        }
    row = await availability_alerts.create_alert(
        user.id,
        body.volumeId,
        title=body.title,
        author=body.author,
        cover_url=body.coverUrl,
    )
    return {
        "watching": True,
        "alreadyAvailable": False,
        "volumeId": row.google_volume_id,
    }


@router.delete("/availability-alerts/{volume_id:path}")
async def delete_availability_alert(
    volume_id: str,
    user: User = Depends(get_current_user),
):
    removed = await availability_alerts.delete_alert(user.id, volume_id)
    return {"watching": False, "removed": removed}


@router.get("/rating/{volume_id:path}")
async def get_book_rating(
    volume_id: str,
    _user: User = Depends(get_current_user),
):
    """Fetch Hardcover community rating for a book (by ISBN or title). Falls back to Goodreads scrape."""
    empty = {"goodreadsRating": 0, "goodreadsCount": 0, "goodreadsReviewCount": 0, "source": "none"}
    book = await _resolve_book_meta(volume_id)
    if not book:
        return empty

    title = book.get("title", "")
    author = (book.get("authors", []) or [""])[0]
    isbn13 = book.get("isbn13", "")
    isbn10 = book.get("isbn10", "")

    result = await hardcover.get_rating(
        isbn13=isbn13, isbn10=isbn10, title=title, author=author,
    )
    if result and result.get("goodreadsRating"):
        return result

    # Fallback only when Hardcover has no hit / no key configured.
    gr = await goodreads.get_rating(
        isbn13=isbn13, isbn10=isbn10, title=title, author=author,
    )
    if gr:
        gr = dict(gr)
        gr.setdefault("source", "goodreads")
        return gr
    return empty


@router.get("/series/{volume_id:path}")
async def get_book_series(
    volume_id: str,
    _user: User = Depends(get_current_user),
):
    """Return ordered books in the same series (Hardcover first, title-detect fallback)."""
    empty = {"seriesName": None, "books": [], "currentBookIndex": -1}
    book = await _resolve_book_meta(volume_id)
    if not book:
        return empty

    title = book.get("title", "")
    subtitle = book.get("subtitle", "")
    authors = book.get("authors", [])
    author = authors[0] if authors else ""
    series_hint = (book.get("seriesName") or "").strip()

    hc = await hardcover.get_series_for_book(
        title=title, author=author, series_hint=series_hint,
    )
    if hc.get("books") and len(hc["books"]) >= 2:
        remapped = await hardcover.resolve_store_volumes(hc["books"])
        remapped = await _annotate_availability(remapped)
        # Recompute current index against remapped ids / titles.
        current_idx = -1
        title_n = _norm_title_key(title)
        for i, b in enumerate(remapped):
            bid = b.get("id") or b.get("volumeId")
            if bid == volume_id:
                current_idx = i
                break
            if title_n and _norm_title_key(b.get("title") or "") == title_n:
                current_idx = i
                break
        return {
            "seriesName": hc.get("seriesName"),
            "books": [
                {
                    "id": b.get("id") or b.get("volumeId"),
                    "title": b.get("title", ""),
                    "subtitle": b.get("subtitle", ""),
                    "coverUrl": b.get("coverUrl", ""),
                    "authors": b.get("authors", []),
                    "sequence": b.get("sequence") or b.get("seriesBookNumber") or "",
                    "publishedDate": b.get("publishedDate", ""),
                    "availability": b.get("availability"),
                }
                for b in remapped
            ],
            "currentBookIndex": current_idx,
            "source": "hardcover",
        }

    # Legacy title-detection fallback (Google/OL search).
    return await _series_from_title_detect(book, volume_id)


@router.get("/curated/{slug}")
async def curated_genre_shelf(
    slug: str,
    pageSize: int = Query(20, ge=1, le=40),
    _user: User = Depends(get_current_user),
):
    """Hardcover curated community list for a genre shelf."""
    cache_key = f"curated:{slug}:{pageSize}"
    cached = _shelf_get(cache_key)
    if cached is not None:
        return cached

    genre = google_books.get_genre_info(slug)
    fallback_name = (
        genre.get("name", slug.replace("-", " ").title()) if genre
        else slug.replace("-", " ").title()
    )

    try:
        shelf = await hardcover.get_curated_shelf(slug, limit=pageSize)
        hc_books = shelf.get("books") or []
        # Remap onto local catalog ids when possible, but keep Hardcover order.
        # ISBN-only remap keeps home shelves snappy; title FTS for every list
        # book made cold loads feel random/stuck.
        books = await hardcover.resolve_store_volumes(hc_books, quick=True) if hc_books else []
        books = await _annotate_availability(books)
        list_name = (shelf.get("listName") or "").strip() or fallback_name
        source = shelf.get("source") or ("hardcover" if books else "none")
        payload = {
            "books": books,
            "totalItems": len(books),
            "page": 1,
            "pageSize": pageSize,
            "category": list_name,
            "genre": fallback_name,
            "listName": list_name,
            "listId": shelf.get("listId"),
            "source": source,
            "availableOnly": False,
        }
        if books:
            _shelf_put(cache_key, payload)
        return payload
    except Exception as e:
        logger.warning("curated shelf failed for %s: %s", slug, e)
        return {
            "books": [],
            "totalItems": 0,
            "page": 1,
            "pageSize": pageSize,
            "category": fallback_name,
            "genre": fallback_name,
            "listName": fallback_name,
            "source": "none",
            "availableOnly": False,
        }


@router.get("/home-shelves")
async def home_curated_shelves(
    page: int = Query(1, ge=1),
    pageSize: int = Query(6, ge=1, le=12),
    booksPerShelf: int = Query(12, ge=1, le=20),
    _user: User = Depends(get_current_user),
):
    """Paginated curated recommendation shelves for the main store (not plain genres)."""
    all_entries = list(hardcover.HOME_CURATED_SHELVES)
    total = len(all_entries)
    start = (page - 1) * pageSize
    end = start + pageSize
    page_entries = all_entries[start:end]
    cache_key = f"home-shelves:v2:{page}:{pageSize}:{booksPerShelf}"
    cached = _shelf_get(cache_key)
    if cached is not None:
        return cached

    # Single-flight per page — concurrent home loads must not stampede Hardcover.
    async with _home_shelves_lock:
        cached = _shelf_get(cache_key)
        if cached is not None:
            return cached

        shelves: list[dict] = []
        for entry in page_entries:
            slug = entry["slug"]
            one = await curated_genre_shelf(slug, pageSize=booksPerShelf, _user=_user)
            shelves.append({
                "slug": slug,
                "title": one.get("listName") or one.get("category") or slug,
                "genre": entry.get("genre") or one.get("genre") or slug,
                "listName": one.get("listName") or "",
                "source": one.get("source") or "none",
                "books": one.get("books") or [],
            })
            await asyncio.sleep(0.35)

        payload = {
            "shelves": shelves,
            "source": "hardcover",
            "page": page,
            "pageSize": pageSize,
            "totalShelves": total,
            "hasMore": end < total,
        }
        if any(s.get("books") for s in shelves):
            _shelf_put(cache_key, payload)
        return payload


@router.get("/curated-slugs")
async def list_curated_slugs(_user: User = Depends(get_current_user)):
    """Slugs that ShelfPage should load via /curated rather than genre browse."""
    return {"slugs": sorted(hardcover.curated_shelf_slugs())}


async def _resolve_book_meta(volume_id: str) -> dict | None:
    if volume_id.startswith("cache:"):
        return await indexer_cache.get_cache_release_detail(volume_id[6:])
    if volume_id.startswith("HC:"):
        raw = volume_id[3:]
        if raw.isdigit():
            book = await hardcover.get_book_by_id(int(raw))
            if book:
                return await hardcover.resolve_store_volume(book)
        return None
    if volume_id.startswith("OL:"):
        return await google_books.get_open_library_work(volume_id[3:])
    if volume_id.startswith("ISBN:"):
        return await google_books.get_catalog_volume(volume_id)
    return await google_books.get_volume(volume_id)


async def _series_from_title_detect(book: dict, volume_id: str) -> dict:
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
        return {"seriesName": None, "books": [], "currentBookIndex": -1, "source": "none"}

    search_q = f"{series_name} {author}".strip()
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
        elif (
            len(series_name) >= 6
            and (
                b_title.lower().startswith(series_name.lower())
                or b_full.lower().startswith(series_name.lower())
            )
        ):
            # Only accept prefix matches — loose substring matching pulled in
            # unrelated books that merely mentioned the series name.
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
        return {"seriesName": None, "books": [], "currentBookIndex": -1, "source": "none"}

    return {
        "seriesName": series_name,
        "books": series_books,
        "currentBookIndex": current_idx,
        "source": "title_detect",
    }


def _normalize_volume_id(volume_id: str) -> str:
    """Normalize catalog ids so OL work keys always resolve as /works/OL…W."""
    if not volume_id.startswith("OL:"):
        return volume_id
    key = volume_id[3:].strip()
    if not key:
        return volume_id
    if key.startswith("/"):
        return f"OL:{key}"
    if key.startswith("works/") or key.startswith("books/") or key.startswith("authors/"):
        return f"OL:/{key}"
    # Bare work id from a truncated URL (e.g. OL123W)
    if re.fullmatch(r"OL\d+W", key, flags=re.IGNORECASE):
        return f"OL:/works/{key}"
    return f"OL:/{key}" if "/" not in key else f"OL:{key}"


@router.get("/{volume_id:path}")
async def get_book_detail(
    volume_id: str,
    _user: User = Depends(get_current_user),
):
    volume_id = _normalize_volume_id(volume_id)
    if volume_id.startswith("cache:"):
        book = await indexer_cache.get_cache_release_detail(volume_id[6:])
        if not book:
            raise HTTPException(status_code=404, detail="Cached release not found")
        return book
    if volume_id.startswith("HC:"):
        book = await _resolve_book_meta(volume_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        return book
    # Open Library / ISBNdb / legacy Google — one path with cover enrichment.
    book = await google_books.get_catalog_volume(volume_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book
