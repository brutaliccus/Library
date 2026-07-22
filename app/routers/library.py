"""User streaming library: curated collection of books with RD streaming."""

import hashlib
import asyncio
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi.responses import FileResponse, HTMLResponse, Response

from app.database import get_db, async_session
from app.models import User, StreamingLibraryItem, DownloadRequest
from app.utils.auth import get_current_user
from app.services import debrid, debrid_tokens, audiobookshelf, kavita, google_books, hardcover
from app.services import kavita_ebook_match
from app.services.google_books import GENRE_TAXONOMY
from app.utils.book_series import (
    is_junk_library_label,
    is_junk_series_hint,
    library_series_from_title,
    parse_abs_series_label,
)
from app.routers.stream import tracks_with_stable_urls

logger = logging.getLogger(__name__)

# Build a lookup: lowercased sub-genre name/keyword -> top-level genre name
# Same taxonomy as store `/books/genres` (GENRE_TAXONOMY).
_GENRE_TO_TOPLEVEL: dict[str, str] = {}
_TAXONOMY_TOP_NAMES: set[str] = set()


def _build_genre_lookup() -> None:
    if _GENRE_TO_TOPLEVEL:
        return
    for top in GENRE_TAXONOMY:
        top_name = top["name"]
        _TAXONOMY_TOP_NAMES.add(top_name)
        _GENRE_TO_TOPLEVEL[top_name.lower()] = top_name
        _GENRE_TO_TOPLEVEL[top["slug"]] = top_name
        for child in top.get("children", []):
            _GENRE_TO_TOPLEVEL[child["name"].lower()] = top_name
            _GENRE_TO_TOPLEVEL[child["slug"]] = top_name
            for word in child["name"].lower().replace("/", " ").replace("&", " ").split():
                if word not in ("of", "the", "and", "a", "an", "in", "ya"):
                    _GENRE_TO_TOPLEVEL.setdefault(word, top_name)


def _map_to_toplevel(genre: str) -> str | None:
    """Map a source genre to a store top-level taxonomy name, or None if junk/unknown."""
    _build_genre_lookup()
    low = (genre or "").lower().strip()
    if not low or is_junk_library_label(low):
        return None
    if low in _GENRE_TO_TOPLEVEL:
        return _GENRE_TO_TOPLEVEL[low]
    # Prefer longer key matches so "science fiction" beats "fiction" fragments
    best: tuple[int, str] | None = None
    for key, val in _GENRE_TO_TOPLEVEL.items():
        if len(key) < 4:
            continue
        if key in low or low in key:
            score = len(key)
            if best is None or score > best[0]:
                best = (score, val)
    return best[1] if best else None


def _normalize_item_genres(raw_genres: list) -> list[str]:
    """Collapse ABS/Kavita genres onto store taxonomy tops; drop media-type junk."""
    out: list[str] = []
    seen: set[str] = set()
    for g in raw_genres or []:
        try:
            if isinstance(g, str):
                label = g
            elif isinstance(g, dict):
                label = g.get("name") or g.get("title") or g.get("tag") or ""
            else:
                label = str(g) if g is not None else ""
            top = _map_to_toplevel(str(label))
            if top and top not in seen:
                seen.add(top)
                out.append(top)
        except Exception:
            continue
    return out


def _local_series_from_item(item: dict) -> tuple[str, str]:
    """Return (series_name, sequence) from local item fields / title — never Hardcover.

    Prefers seriesName, then series[] entries, then title-inferred labels.
    Junk ASINs / Amazon noise / media-type labels are skipped.
    ABS Folder Forge labels like ``Dungeon Crawler Carl #1`` are split into
    name + sequence so filters/grouping share one series bucket.
    """
    sn = (item.get("seriesName") or "").strip()
    if sn:
        name, seq = parse_abs_series_label(sn)
        if name:
            return name, str(item.get("sequence") or seq or "").strip()
    for s in item.get("series") or []:
        if isinstance(s, dict):
            name = (s.get("name") or "").strip()
            seq = str(s.get("sequence") or "").strip()
        else:
            name = (str(s) if s is not None else "").strip()
            seq = ""
        if not name:
            continue
        parsed_name, parsed_seq = parse_abs_series_label(name)
        if parsed_name:
            return parsed_name, seq or parsed_seq
    inferred = library_series_from_title(item.get("title") or "")
    if inferred and not is_junk_series_hint(inferred[0]):
        return inferred[0], str(inferred[1] or "").strip()
    return "", ""


def _apply_local_series_fields(item: dict) -> dict:
    """Stamp seriesName/sequence from local metadata so clients can filter offline."""
    sname, seq = _local_series_from_item(item)
    if not sname:
        return item
    out = {**item, "seriesName": sname, "sequence": seq or item.get("sequence") or ""}
    series_bits = out.get("series") or []
    if not series_bits:
        out["series"] = [{"name": sname, "sequence": seq}]
    return out


def _series_hint_from_item(item: dict) -> str:
    """Clean local series label (for optional Hardcover genre match hints)."""
    return _local_series_from_item(item)[0]


# Soft budget so collection shelves stay fast when Hardcover is slow/down.
_ENRICH_BUDGET_SECONDS = 8.0


async def _enrich_items_via_hardcover(
    items: list[dict],
    *,
    title_key: str = "title",
    author_key: str = "author",
    concurrency: int = 8,
    budget_seconds: float = _ENRICH_BUDGET_SECONDS,
) -> list[dict]:
    """Enrich library items with Hardcover *genres* only (taxonomy-mapped).

    Author and series stay on local ABS/Kavita/PC metadata — never overwritten.
    Fail-open: any HC error/timeout returns the original items unchanged (or
    whatever finished before the budget). Never raises into collection handlers.
    """
    if not items:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(item: dict) -> dict:
        try:
            title = (item.get(title_key) or "").strip()
            if not title:
                return item
            author = (item.get(author_key) or "").strip()
            hint = _series_hint_from_item(item)
            async with sem:
                hc = await hardcover.match_library_book(
                    title=title, author=author, series_hint=hint,
                )
            if not isinstance(hc, dict):
                return item
            out = {**item}
            hc_genres = _normalize_item_genres(hc.get("genres") or [])
            if hc_genres:
                out["genres"] = hc_genres
                # Personal Collection uses singular genre for filters/UI.
                if "genre" in item:
                    out["genre"] = hc_genres[0]
            return out
        except Exception:
            logger.debug(
                "Hardcover enrich failed for %s",
                (item.get(title_key) if isinstance(item, dict) else None) or "?",
                exc_info=True,
            )
            return item

    async def _run() -> list[dict]:
        results = await asyncio.gather(*[_one(it) for it in items], return_exceptions=True)
        out: list[dict] = []
        for original, result in zip(items, results):
            if isinstance(result, dict):
                out.append(result)
            else:
                if isinstance(result, BaseException):
                    logger.debug("Hardcover enrich gather item failed", exc_info=result)
                out.append(original)
        return out

    try:
        if budget_seconds and budget_seconds > 0:
            return await asyncio.wait_for(_run(), timeout=budget_seconds)
        return await _run()
    except asyncio.TimeoutError:
        logger.warning(
            "Hardcover enrichment timed out after %.1fs (%d items); returning unenriched",
            budget_seconds,
            len(items),
        )
        return items
    except Exception:
        logger.exception(
            "Hardcover enrichment failed entirely (%d items); returning unenriched",
            len(items),
        )
        return items


def _group_items_by_local_series(
    items: list[dict],
    *,
    id_key: str = "itemId",
) -> list[dict]:
    """Group library items by local series metadata (ABS/Kavita/title fields).

    Only series with 2+ library books are returned. No Hardcover calls.
    """
    if not items:
        return []

    groups: dict[str, dict] = {}
    for item in items:
        sname, seq = _local_series_from_item(item)
        if not sname:
            continue
        key = sname.lower()
        bucket = groups.setdefault(
            key,
            {
                "id": f"local:{key}",
                "name": sname,
                "books": [],
                "bookCount": 0,
                "totalDuration": 0,
                "coverUrl": "",
                "_seen": set(),
            },
        )
        iid = item.get(id_key)
        if iid is None or iid in bucket["_seen"]:
            continue
        bucket["_seen"].add(iid)
        book = {**item, "seriesName": sname, "sequence": seq or item.get("sequence") or ""}
        # SeriesDrilldown expects itemId for ABS play
        if "itemId" not in book and id_key != "itemId" and item.get(id_key) is not None:
            book["itemId"] = str(item.get(id_key))
        bucket["books"].append(book)
        if not bucket["coverUrl"] and book.get("coverUrl"):
            bucket["coverUrl"] = book["coverUrl"]

    series_list: list[dict] = []
    for bucket in groups.values():
        books = bucket.pop("books")
        bucket.pop("_seen", None)
        try:
            books.sort(key=lambda b: float(b.get("sequence") or "999"))
        except (ValueError, TypeError):
            books.sort(key=lambda b: str(b.get("sequence") or ""))
        if len(books) < 2:
            continue
        bucket["books"] = books
        bucket["bookCount"] = len(books)
        bucket["totalDuration"] = round(
            sum(float(b.get("duration") or 0) for b in books)
        )
        series_list.append(bucket)

    series_list.sort(key=lambda s: s["name"].lower())
    return series_list

router = APIRouter(prefix="/api/library", tags=["library"])

AUDIO_EXT = re.compile(r"\.(mp3|m4a|m4b|ogg|opus|flac|wav|wma|aac|mp4)$", re.IGNORECASE)
ARCHIVE_EXT = re.compile(r"\.(rar|zip|7z|tar|gz|bz2|r\d{2})$", re.IGNORECASE)


class AddToLibraryRequest(BaseModel):
    google_volume_id: str
    title: str
    author: str = ""
    cover_url: str = ""
    genre: str = ""
    magnet_link: Optional[str] = None


class ResolveStreamRequest(BaseModel):
    magnet_link: str
    title: str = "Unknown"


class FormatMatchesRequest(BaseModel):
    titles: list[str]


class UpdateProgressRequest(BaseModel):
    progress_seconds: float
    total_seconds: float = 0


async def _upsert_library_item(
    session: AsyncSession,
    user_id: int,
    vid: str,
    title: str,
    author: str,
    cover_url: str,
    genre: str,
    magnet_link: Optional[str],
    rd_torrent_id: Optional[str],
    tracks: Optional[list[dict]],
    provider: str = "rd",
) -> StreamingLibraryItem:
    existing = await session.execute(
        select(StreamingLibraryItem).where(
            and_(
                StreamingLibraryItem.user_id == user_id,
                StreamingLibraryItem.google_volume_id == vid,
            )
        )
    )
    item = existing.scalar_one_or_none()
    if item:
        if magnet_link and not item.magnet_link:
            item.magnet_link = magnet_link
        if rd_torrent_id:
            item.rd_torrent_id = rd_torrent_id
            item.debrid_provider = provider or "rd"
        if tracks:
            item.tracks_json = json.dumps(tracks)
            item.stream_status = "ready"
        await session.commit()
        await session.refresh(item)
        return item
    item = StreamingLibraryItem(
        user_id=user_id,
        google_volume_id=vid,
        title=title,
        author=author,
        cover_url=cover_url,
        genre=genre,
        magnet_link=magnet_link,
        rd_torrent_id=rd_torrent_id,
        debrid_provider=provider or "rd",
        tracks_json=json.dumps(tracks) if tracks else None,
        stream_status="ready" if tracks else "added",
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def add_to_library_from_stream(
    user_id: int,
    title: str,
    author: str,
    cover_url: str = "",
    genre: str = "",
    magnet_link: Optional[str] = None,
    google_volume_id: Optional[str] = None,
    rd_torrent_id: Optional[str] = None,
    tracks: Optional[list[dict]] = None,
    db: Optional[AsyncSession] = None,
    provider: str = "rd",
) -> Optional[StreamingLibraryItem]:
    """Add a stream/request to the user's Personal Collection. Uses google_volume_id if provided,
    else a stable hash of title|author. When tracks/torrent id are provided (from a stream
    resolve), they're stored so the item is instantly playable without re-resolving."""
    vid = google_volume_id
    if not vid:
        vid = "rd:" + hashlib.sha256(f"{title}|{author}".encode()).hexdigest()[:28]
    if db is not None:
        return await _upsert_library_item(
            db, user_id, vid, title, author, cover_url, genre,
            magnet_link, rd_torrent_id, tracks, provider,
        )
    async with async_session() as session:
        try:
            return await _upsert_library_item(
                session, user_id, vid, title, author, cover_url, genre,
                magnet_link, rd_torrent_id, tracks, provider,
            )
        except Exception as e:
            logger.warning("add_to_library_from_stream failed: %s", e)
            return None


async def _personal_collection_dicts(
    user: User,
    db: AsyncSession,
) -> list[dict]:
    """Load Personal Collection rows (drop rd: junk), serialize, enrich via Hardcover."""
    stmt = (
        select(StreamingLibraryItem)
        .where(StreamingLibraryItem.user_id == user.id)
        .order_by(StreamingLibraryItem.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    keep: list[StreamingLibraryItem] = []
    dirty = False
    need_cover: list[StreamingLibraryItem] = []
    for item in rows:
        vid = item.google_volume_id or ""
        if vid.startswith("rd:"):
            # Legacy stream/request auto-adds — remove so PC stays curated.
            await db.delete(item)
            dirty = True
            continue
        if item.genre:
            mapped = _map_to_toplevel(item.genre)
            if mapped and mapped != item.genre:
                item.genre = mapped
                dirty = True
            elif mapped is None and is_junk_library_label(item.genre):
                item.genre = ""
                dirty = True
        if not (item.cover_url or "").strip() and vid:
            need_cover.append(item)
        keep.append(item)

    if need_cover:
        async def _fill(it: StreamingLibraryItem) -> None:
            nonlocal dirty
            cover = await _lookup_cover_for_volume(
                it.google_volume_id, it.title, it.author or ""
            )
            if cover:
                it.cover_url = cover
                dirty = True

        await asyncio.gather(*[_fill(it) for it in need_cover[:12]])

    if dirty:
        await db.commit()
    serialized = [_apply_local_series_fields(_serialize(item)) for item in keep]
    # Stamp genres array from stored genre so offline filters work even if HC skips.
    for row in serialized:
        if not row.get("genres"):
            g = (row.get("genre") or "").strip()
            mapped = _map_to_toplevel(g) if g else None
            row["genres"] = [mapped] if mapped else ([g] if g else [])
    return await _enrich_items_via_hardcover(serialized)


@router.get("")
async def get_library(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Personal Collection — explicit adds only (synthetic rd: stream autos are hidden)."""
    return {"items": await _personal_collection_dicts(user, db)}


@router.get("/series")
async def personal_series(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Group Personal Collection by local series metadata (title / seriesName)."""
    items = await _personal_collection_dicts(user, db)
    return {"series": _group_items_by_local_series(items, id_key="id")}


@router.post("")
async def add_to_library(
    body: AddToLibraryRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(StreamingLibraryItem).where(
            and_(
                StreamingLibraryItem.user_id == user.id,
                StreamingLibraryItem.google_volume_id == body.google_volume_id,
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already in your library")

    item = StreamingLibraryItem(
        user_id=user.id,
        google_volume_id=body.google_volume_id,
        title=body.title,
        author=body.author,
        cover_url=body.cover_url,
        genre=body.genre,
        magnet_link=body.magnet_link,
        stream_status="added",
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _serialize(item)


@router.delete("/{item_id}")
async def remove_from_library(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await _get_user_item(item_id, user.id, db)
    await db.delete(item)
    await db.commit()
    return {"status": "removed"}


@router.post("/{item_id}/resolve")
async def resolve_library_stream(
    item_id: int,
    body: ResolveStreamRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a magnet link via a debrid provider (auto-picked) and store the
    stream tracks on the library item."""
    item = await _get_user_item(item_id, user.id, db)

    item.magnet_link = body.magnet_link
    item.stream_status = "resolving"
    await db.commit()

    await debrid_tokens.apply_tokens_for_user_id(user.id)
    provider = await debrid.pick_provider_for_magnet(
        body.magnet_link, getattr(user, "preferred_debrid", "rd") or "rd"
    )
    client = debrid.get_client(provider)

    try:
        result = await client.add_magnet(body.magnet_link)
        torrent_id = result.get("id")
        if not torrent_id:
            item.stream_status = "error"
            await db.commit()
            raise HTTPException(status_code=502, detail="Debrid service did not return a torrent ID")

        item.rd_torrent_id = torrent_id
        item.debrid_provider = provider
        await db.commit()

        # Inspect torrent files and prefer selecting only audio files
        info_pre = await client.get_torrent_info(torrent_id)
        rd_files = info_pre.get("files", [])
        audio_file_ids = []
        archive_names = []
        for f in rd_files:
            path = f.get("path", "")
            fname = path.rsplit("/", 1)[-1] if "/" in path else path
            fid = f.get("id")
            if AUDIO_EXT.search(fname):
                audio_file_ids.append(str(fid))
            elif ARCHIVE_EXT.search(fname):
                archive_names.append(fname)

        if audio_file_ids:
            await client.select_files(torrent_id, ",".join(audio_file_ids))
        elif archive_names:
            item.stream_status = "error"
            await db.commit()
            raise HTTPException(
                status_code=422,
                detail=(
                    f"This torrent contains compressed files ({', '.join(archive_names[:3])}). "
                    "Compressed archives can't be streamed — use the Request button to download and extract instead."
                ),
            )
        else:
            await client.select_files(torrent_id, "all")

        info = await client.poll_until_ready(torrent_id, interval=3, timeout=300)

        links = info.get("links", [])
        if not links:
            item.stream_status = "error"
            await db.commit()
            raise HTTPException(status_code=502, detail="No files from debrid service")

        unrestricted = await asyncio.gather(
            *[client.unrestrict_link(link) for link in links],
            return_exceptions=True,
        )

        tracks = []
        has_archives = False
        for i, url in enumerate(unrestricted):
            if isinstance(url, Exception):
                continue
            filename = debrid.link_filename(links[i], url)
            if ARCHIVE_EXT.search(filename):
                has_archives = True
                continue
            if not AUDIO_EXT.search(filename):
                continue
            name = AUDIO_EXT.sub("", filename).replace("_", " ").replace("-", " ").replace(".", " ").strip()
            tracks.append({
                "index": len(tracks),
                "startOffset": 0,
                "duration": 0,
                "title": name or filename,
                "contentUrl": url,
                "mimeType": "audio/mpeg",
            })

        if not tracks:
            item.stream_status = "error"
            await db.commit()
            if has_archives:
                raise HTTPException(
                    status_code=422,
                    detail="This torrent contains compressed files that can't be streamed. Use the Request button to download and extract instead.",
                )
            raise HTTPException(status_code=404, detail="No audio files found")

        item.tracks_json = json.dumps(tracks)
        item.stream_status = "ready"
        await db.commit()
        await db.refresh(item)
        return _serialize(item)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Library resolve failed: %s", e, exc_info=True)
        item.stream_status = "error"
        await db.commit()
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{item_id}/progress")
async def update_progress(
    item_id: int,
    body: UpdateProgressRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await _get_user_item(item_id, user.id, db)
    item.progress_seconds = body.progress_seconds
    if body.total_seconds > 0:
        item.total_seconds = body.total_seconds
    await db.commit()
    return {"status": "ok"}


@router.post("/{item_id}/play")
async def play_library_item(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Prepare a library item for playback: returns tracks plus a StreamHistory id
    so the player can sync progress correctly (the library item id is NOT a history id)."""
    from app.routers.stream import _save_stream_history

    item = await _get_user_item(item_id, user.id, db)
    if not item.tracks_json:
        raise HTTPException(status_code=409, detail="Item has no resolved stream yet")
    try:
        raw_tracks = json.loads(item.tracks_json)
    except Exception:
        raise HTTPException(status_code=409, detail="Item tracks are corrupted")

    task: dict = {}
    await _save_stream_history(
        task, user.id, item.title, item.author, item.cover_url,
        item.magnet_link, None, None, item.rd_torrent_id, raw_tracks,
    )
    history_id = task.get("stream_history_id")
    tracks = (
        tracks_with_stable_urls("h", history_id, raw_tracks)
        if history_id
        else tracks_with_stable_urls("l", item.id, raw_tracks)
    )
    return {
        "tracks": tracks,
        "streamHistoryId": history_id,
        "title": item.title,
        "author": item.author,
        "coverUrl": item.cover_url,
        "progressSeconds": task.get("progress_seconds", 0),
        "currentTrackIndex": task.get("current_track_index", 0),
        "trackPositionSeconds": task.get("track_position_seconds", 0),
    }


@router.get("/abs/collection")
async def abs_collection(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all ABS library items grouped by store top-level genres."""
    hidden_titles = await _get_private_titles_for_others(user.id, db)
    raw_items = [
        it for it in await audiobookshelf.get_all_items()
        if not _is_hidden(it.get("title", ""), hidden_titles)
    ]
    # Drop ABS junk series labels; stamp seriesName from local metadata for filters.
    cleaned: list[dict] = []
    for item in raw_items:
        series_bits = []
        for s in item.get("series") or []:
            name = (s.get("name") or "").strip()
            if name and not is_junk_series_hint(name):
                series_bits.append(s)
        mapped = _normalize_item_genres(item.get("genres") or [])
        cleaned.append(
            _apply_local_series_fields({**item, "genres": mapped, "series": series_bits})
        )
    # Genres-only Hardcover enrich (fail-open); author/series stay local.
    items = await _enrich_items_via_hardcover(cleaned)

    genres: dict[str, list] = {}
    ungrouped: list = []
    seen_in_genre: dict[str, set] = {}
    for item in items:
        mapped = item.get("genres") or []
        if not mapped:
            ungrouped.append(item)
            continue
        for top in mapped:
            seen_in_genre.setdefault(top, set())
            if item["itemId"] not in seen_in_genre[top]:
                genres.setdefault(top, []).append(item)
                seen_in_genre[top].add(item["itemId"])
    visible = sum(len(v) for v in genres.values()) + len(ungrouped)
    sorted_genres = dict(sorted(genres.items(), key=lambda x: x[0]))
    for bucket in sorted_genres.values():
        bucket.sort(key=lambda x: x.get("addedAt") or 0, reverse=True)
    ungrouped.sort(key=lambda x: x.get("addedAt") or 0, reverse=True)
    return {
        "genres": sorted_genres,
        "ungrouped": ungrouped,
        "totalItems": visible,
    }


@router.get("/abs/series")
async def abs_series(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Group audiobooks by local series metadata (ABS series fields / title cues).

    Junk Amazon/ASIN labels are skipped. No Hardcover lookups.
    """
    hidden_titles = await _get_private_titles_for_others(user.id, db)
    items = []
    for it in await audiobookshelf.get_all_items():
        if _is_hidden(it.get("title", ""), hidden_titles):
            continue
        series_bits = [
            s for s in (it.get("series") or [])
            if (s.get("name") or "").strip() and not is_junk_series_hint(s.get("name") or "")
        ]
        items.append(_apply_local_series_fields({**it, "series": series_bits}))
    return {"series": _group_items_by_local_series(items, id_key="itemId")}


@router.get("/kavita/series")
async def kavita_series_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Group ebooks by local series metadata (title / seriesName on collection items)."""
    # Reuse collection builder so chapter/cover fields stay consistent
    coll = await kavita_collection(user=user, db=db)
    items = coll.get("items") or []
    return {"series": _group_items_by_local_series(items, id_key="seriesId")}


@router.get("/abs/item/{item_id}")
async def abs_item_detail(
    item_id: str,
    _user: User = Depends(get_current_user),
):
    """Full metadata for one ABS item — powers the library book detail page."""
    item = await audiobookshelf.get_library_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    media = item.get("media", {})
    meta = media.get("metadata", {})
    series_list = meta.get("series", [])
    if isinstance(series_list, dict):
        series_list = [series_list]
    title = meta.get("title") or ""
    author = meta.get("authorName") or ""
    abs_genres = _normalize_item_genres(meta.get("genres") or [])
    abs_series = [
        {"id": s.get("id", ""), "name": s.get("name", ""), "sequence": s.get("sequence", "")}
        for s in series_list
        if (s.get("name") or "").strip() and not is_junk_series_hint(s.get("name") or "")
    ]
    series_hint = ""
    if abs_series:
        series_hint = abs_series[0]["name"]
    else:
        inferred = library_series_from_title(title)
        if inferred and not is_junk_series_hint(inferred[0]):
            series_hint = inferred[0]

    try:
        hc = await hardcover.match_library_book(
            title=title, author=author, series_hint=series_hint,
        )
    except Exception:
        logger.debug("Hardcover match failed for ABS item %s", item_id, exc_info=True)
        hc = {}

    hc_author = (hc.get("author") or "").strip()
    hc_genres = _normalize_item_genres(hc.get("genres") or [])
    hc_series_name = (hc.get("seriesName") or "").strip()
    hc_seq = str(hc.get("sequence") or "").strip()
    if hc_series_name and not is_junk_series_hint(hc_series_name):
        out_series = [{"id": "", "name": hc_series_name, "sequence": hc_seq}]
    else:
        out_series = abs_series

    return {
        "itemId": item.get("id", ""),
        "title": title,
        "subtitle": meta.get("subtitle") or "",
        "author": hc_author or author,
        "narrator": meta.get("narratorName") or "",
        "description": meta.get("description") or "",
        "publisher": meta.get("publisher") or "",
        "publishedYear": meta.get("publishedYear") or "",
        "genres": hc_genres or abs_genres,
        "series": out_series,
        "duration": media.get("duration", 0) or 0,
        "numTracks": media.get("numTracks", 0) or media.get("numAudioFiles", 0) or 0,
        "coverUrl": f"/api/stream/abs/proxy/cover/{item_id}",
    }


@router.get("/kavita/collection")
async def kavita_collection(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all Kavita ebook series (EPUB/PDF) for the library view."""
    hidden_titles = await _get_private_titles_for_others(user.id, db)
    series = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
    # Fetch volumes + metadata in parallel (limit 10 concurrent) to avoid 504 timeout
    sem = asyncio.Semaphore(10)

    async def volumes_and_meta(s: dict) -> tuple[dict, list, dict]:
        sid = s.get("id", 0)
        async with sem:
            volumes, meta = await asyncio.gather(
                kavita.get_series_volumes(sid),
                kavita.get_series_metadata(sid),
            )
        return s, volumes, meta or {}

    results = await asyncio.gather(*[volumes_and_meta(s) for s in series])
    items: list[dict] = []
    for s, volumes, meta in results:
        name = s.get("name") or s.get("localizedName") or s.get("originalName") or ""
        if not name or _is_hidden(name, hidden_titles):
            continue
        book_num = kavita_ebook_match._book_number_from_text(name)
        chapter_id = kavita_ebook_match._pick_chapter_id(volumes, book_num)
        volume_id: int | None = None
        for vol in volumes:
            chapters = vol.get("chapters", [])
            if chapters and chapters[0].get("id") == chapter_id:
                volume_id = vol.get("id")
                break
            if chapter_id is None and chapters:
                chapter_id = chapters[0].get("id")
                volume_id = vol.get("id")
                break
        sid = s.get("id")
        cover_url = f"/api/library/reader/cover/ebook?seriesId={sid}" if sid else ""
        if cover_url and volume_id:
            cover_url += f"&volumeId={volume_id}"
        if cover_url and chapter_id:
            cover_url += f"&chapterId={chapter_id}"

        writers = meta.get("writers") or s.get("authors") or []
        author = ""
        if writers:
            author = (writers[0] or {}).get("name", "") if isinstance(writers[0], dict) else str(writers[0])
        genres = _normalize_item_genres(meta.get("genres") or [])
        # Local series from title cues (LibraForge-cleaned titles) — no Hardcover.
        inferred = library_series_from_title(name)
        sname = ""
        seq = ""
        if inferred and not is_junk_series_hint(inferred[0]):
            sname, seq = inferred[0], str(inferred[1] or "")

        added_at = s.get("created") or s.get("lastChapterAdded") or meta.get("releaseYear") or 0
        try:
            # Kavita may return ISO strings or epoch ms
            if isinstance(added_at, str) and added_at:
                from datetime import datetime
                added_ms = int(datetime.fromisoformat(added_at.replace("Z", "+00:00")).timestamp() * 1000)
            else:
                added_ms = int(added_at or 0)
                if added_ms < 10_000_000_000:  # seconds → ms
                    added_ms = added_ms * 1000 if added_ms else 0
        except Exception:
            added_ms = 0

        items.append({
            "seriesId": s.get("id"),
            "title": name,
            "author": author,
            "coverUrl": cover_url,
            "chapterId": chapter_id,
            "genres": genres,
            "seriesName": sname,
            "sequence": seq,
            "series": [{"name": sname, "sequence": seq}] if sname else [],
            "addedAt": added_ms,
            "source": "kavita",
        })
    # Genres-only Hardcover enrich (fail-open); author/series stay local.
    items = await _enrich_items_via_hardcover(items)
    # Newest first by default for the All shelf
    items.sort(key=lambda x: x.get("addedAt") or 0, reverse=True)
    return {"items": items, "totalItems": len(items)}


@router.get("/search")
async def search_library_unified(
    q: str = Query("", min_length=1),
    media: str = Query("all", description="Filter: all, audiobooks, ebooks"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search across ABS, Kavita ebooks, and user's RD streaming library."""
    hidden_titles = await _get_private_titles_for_others(user.id, db)
    results = []
    seen_abs_ids: set[str] = set()
    seen_kavita_ids: set[int] = set()
    q_lower = q.lower()

    include_audiobooks = media in ("all", "audiobooks")
    include_ebooks = media in ("all", "ebooks")

    if include_audiobooks:
        abs_items = await audiobookshelf.search_library_with_ids(q)
        for item in abs_items:
            iid = item.get("itemId", "")
            if _is_hidden(item.get("title", ""), hidden_titles):
                continue
            seen_abs_ids.add(iid)
            results.append({
                "title": item.get("title", ""),
                "author": item.get("author", ""),
                "coverUrl": item.get("coverUrl", ""),
                "source": "abs",
                "itemId": iid,
            })

        all_abs = await audiobookshelf.get_all_items()
        for item in all_abs:
            iid = item.get("itemId", "")
            if iid in seen_abs_ids:
                continue
            author = item.get("author", "")
            title = item.get("title", "")
            if _is_hidden(title, hidden_titles):
                continue
            if q_lower in author.lower() or q_lower in title.lower():
                seen_abs_ids.add(iid)
                results.append({
                    "title": title,
                    "author": author,
                    "coverUrl": item.get("coverUrl", ""),
                    "source": "abs",
                    "itemId": iid,
                })

    if include_ebooks:
        kavita_series = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
        for s in kavita_series:
            name = s.get("name") or s.get("localizedName") or s.get("originalName") or ""
            if not name or _is_hidden(name, hidden_titles):
                continue
            author = (s.get("authors") or [{}])[0].get("name", "") if s.get("authors") else ""
            if q_lower not in name.lower() and (not author or q_lower not in author.lower()):
                continue
            sid = s.get("id")
            if sid in seen_kavita_ids:
                continue
            seen_kavita_ids.add(sid)
            volumes = await kavita.get_series_volumes(sid)
            book_num = kavita_ebook_match._book_number_from_text(name)
            chapter_id = kavita_ebook_match._pick_chapter_id(volumes, book_num)
            volume_id: int | None = None
            for vol in volumes:
                chapters = vol.get("chapters", [])
                if chapters and chapters[0].get("id") == chapter_id:
                    volume_id = vol.get("id")
                    break
                if chapter_id is None and chapters:
                    chapter_id = chapters[0].get("id")
                    volume_id = vol.get("id")
                    break
            author = (s.get("authors") or [{}])[0].get("name", "") if s.get("authors") else ""
            cover_url = f"/api/library/reader/cover/ebook?seriesId={sid}" if sid else ""
            if cover_url and volume_id:
                cover_url += f"&volumeId={volume_id}"
            if cover_url and chapter_id:
                cover_url += f"&chapterId={chapter_id}"
            results.append({
                "title": name,
                "author": author,
                "coverUrl": cover_url,
                "source": "kavita",
                "seriesId": sid,
                "chapterId": chapter_id,
            })

    # RD streaming library: include for "all" or "audiobooks"
    if media in ("all", "audiobooks"):
        stmt = (
            select(StreamingLibraryItem)
            .where(
                and_(
                    StreamingLibraryItem.user_id == user.id,
                    (
                        StreamingLibraryItem.title.ilike(f"%{q}%")
                        | StreamingLibraryItem.author.ilike(f"%{q}%")
                    ),
                )
            )
            .order_by(StreamingLibraryItem.updated_at.desc())
            .limit(20)
        )
        rows = (await db.execute(stmt)).scalars().all()
        for item in rows:
            results.append({
                "title": item.title,
                "author": item.author,
                "coverUrl": item.cover_url,
                "source": "rd",
                "libraryItemId": item.id,
                "googleVolumeId": item.google_volume_id,
                "streamStatus": item.stream_status,
                "tracks": _get_tracks(item),
            })
    return {"results": results}


def _get_tracks(item: StreamingLibraryItem) -> list:
    if not item.tracks_json:
        return []
    try:
        raw = json.loads(item.tracks_json)
        return tracks_with_stable_urls("l", item.id, raw)
    except Exception:
        return []


@router.post("/abs/scan")
async def trigger_abs_scan(user: User = Depends(get_current_user)):
    """Trigger an ABS library scan, wait for completion, then clean orphaned items."""
    try:
        scan_status = await audiobookshelf.scan_library_and_wait()
        await audiobookshelf.remove_items_with_issues()
        audiobookshelf.invalidate_cache()
        return {
            "ok": True,
            "message": (
                "Library scanned and cleaned up"
                if scan_status.get("scan_complete")
                else "Library scan started but did not finish before timeout; refresh again shortly"
            ),
            "scan_ran": bool(scan_status.get("scan_ran")),
            "scan_complete": bool(scan_status.get("scan_complete")),
            "timed_out": bool(scan_status.get("timed_out")),
            "waited_seconds": scan_status.get("waited_seconds"),
            "items_total": scan_status.get("items_total"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/kavita/scan")
async def trigger_kavita_scan(user: User = Depends(get_current_user)):
    """Trigger a Kavita library scan and clear the in-process series cache."""
    try:
        await kavita.scan_library()
        kavita.invalidate_cache()
        return {"ok": True, "message": "Kavita scanned"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --------------- Reader proxy (Kavita Book API) ---------------

@router.get("/reader/cover/ebook")
async def proxy_kavita_ebook_cover(
    series_id: int = Query(..., alias="seriesId", description="Series ID (always required)"),
    volume_id: int | None = Query(None, alias="volumeId"),
    chapter_id: int | None = Query(None, alias="chapterId"),
):
    """Try volume, then chapter, then series cover. Returns first successful image."""
    import httpx
    from app.config import get_settings
    cfg = get_settings()
    urls_to_try: list[tuple[str, str]] = []
    if volume_id:
        urls_to_try.append((f"{cfg.kavita_url}/api/Image/volume-cover?volumeId={volume_id}", "volume"))
    if chapter_id:
        urls_to_try.append((f"{cfg.kavita_url}/api/Image/chapter-cover?chapterId={chapter_id}", "chapter"))
    urls_to_try.append((f"{cfg.kavita_url}/api/Image/series-cover?seriesId={series_id}", "series"))

    headers = {"x-api-key": cfg.kavita_api_key}
    # Image API may require apiKey as query param; append if we have a key
    api_key_param = f"&apiKey={cfg.kavita_api_key}" if cfg.kavita_api_key else ""
    async with httpx.AsyncClient() as client:
        for url, label in urls_to_try:
            try:
                full_url = url + api_key_param if api_key_param and "?" in url else url
                resp = await client.get(full_url, headers=headers, timeout=10)
                if resp.status_code == 200 and len(resp.content) > 0:
                    return Response(
                        content=resp.content,
                        media_type=resp.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
            except Exception as e:
                logger.warning("Kavita %s cover failed: %s", label, e)
    raise HTTPException(status_code=502, detail="No cover available")


@router.get("/reader/cover/volume/{volume_id}")
async def proxy_kavita_volume_cover(volume_id: int):
    """Proxy Kavita volume cover. Tries volume, then chapter (if available), then series."""
    import httpx
    from app.config import get_settings
    cfg = get_settings()
    url = f"{cfg.kavita_url}/api/Image/volume-cover?volumeId={volume_id}"
    if cfg.kavita_api_key:
        url += f"&apiKey={cfg.kavita_api_key}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"x-api-key": cfg.kavita_api_key}, timeout=15)
            resp.raise_for_status()
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch cover")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/reader/cover/series/{series_id}")
async def proxy_kavita_series_cover(series_id: int):
    """Proxy Kavita series cover image."""
    import httpx
    from app.config import get_settings
    cfg = get_settings()
    url = f"{cfg.kavita_url}/api/Image/series-cover?seriesId={series_id}"
    if cfg.kavita_api_key:
        url += f"&apiKey={cfg.kavita_api_key}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"x-api-key": cfg.kavita_api_key}, timeout=15)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("Kavita cover proxy failed: %s", e)
        raise HTTPException(status_code=502, detail="Failed to fetch cover")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/reader/cover/chapter/{chapter_id}")
async def proxy_kavita_chapter_cover(chapter_id: int):
    """Proxy Kavita chapter cover image."""
    import httpx
    from app.config import get_settings
    cfg = get_settings()
    url = f"{cfg.kavita_url}/api/Image/chapter-cover?chapterId={chapter_id}"
    if cfg.kavita_api_key:
        url += f"&apiKey={cfg.kavita_api_key}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"x-api-key": cfg.kavita_api_key}, timeout=15)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("Kavita chapter cover proxy failed: %s", e)
        raise HTTPException(status_code=502, detail="Failed to fetch cover")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/reader/{chapter_id}/book-info")
async def reader_book_info(chapter_id: int):
    """Get EPUB/PDF metadata for the reader. No auth so reader works when token expires mid-session."""
    info = await kavita.get_book_info(chapter_id)
    if not info:
        raise HTTPException(status_code=404, detail="Book info not found")
    return info


@router.get("/reader/{chapter_id}/file")
async def reader_file(chapter_id: int):
    """Stream the source ebook file (EPUB/MOBI/PDF) with Range support for client caching."""
    path = await kavita.get_chapter_file_path(chapter_id)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Book file not found")
    media_types = {
        ".pdf": "application/pdf",
        ".epub": "application/epub+zip",
        ".mobi": "application/x-mobipocket-ebook",
        ".azw3": "application/vnd.amazon.ebook",
        ".cbz": "application/vnd.comicbook+zip",
        ".cbr": "application/vnd.comicbook-rar",
    }
    media_type = media_types.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": "inline",
            "Accept-Ranges": "bytes",
        },
    )


@router.get("/reader/{chapter_id}/pdf")
async def reader_pdf(chapter_id: int):
    """Stream a PDF ebook for in-browser reading. No auth so reader works when token expires mid-session."""
    path = await kavita.get_chapter_file_path(chapter_id)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="PDF not found")
    if path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Chapter is not a PDF")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": "inline",
            "Accept-Ranges": "bytes",
        },
    )


@router.get("/reader/{chapter_id}/chapters")
async def reader_book_chapters(chapter_id: int):
    """Get TOC / page mappings for the reader. No auth so reader works when token expires mid-session."""
    chapters = await kavita.get_book_chapters(chapter_id)
    return chapters


def _prepare_reader_html(html: str) -> str:
    """Strip EPUB author CSS so our reader typography (first-line indent) can win.

    Kavita page HTML often embeds the book's <style> blocks / stylesheet links.
    Those rules are injected into the SPA DOM after our stylesheet and commonly
    reset `text-indent`, which made `.reader-content p { text-indent }` a no-op.
    """
    if not html:
        return html
    out = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.I | re.S)
    out = re.sub(
        r"<link\b[^>]*rel\s*=\s*[\"']?stylesheet[\"']?[^>]*>",
        "",
        out,
        flags=re.I,
    )
    # Drop inline text-indent so author "no indent" styles can't override us.
    out = re.sub(
        r"""(style\s*=\s*["'][^"']*?)text-indent\s*:\s*[^;"']+;?\s*""",
        r"\1",
        out,
        flags=re.I,
    )
    return out


@router.get("/reader/{chapter_id}/book-page")
async def reader_book_page(
    chapter_id: int,
    page: int = Query(..., ge=0),
):
    """Get a single page HTML for the reader. No auth so reader works when token expires mid-session."""
    from app.config import get_settings
    html = await kavita.get_book_page(chapter_id, page)
    if not html:
        raise HTTPException(status_code=404, detail="Page not found")
    html = _prepare_reader_html(html)
    # Rewrite Kavita resource URLs to use our proxy (avoids CORS, mixed content, hides API key)
    cfg = get_settings()
    app_base = (cfg.app_url or "").rstrip("/")
    resource_url = f"{app_base}/api/library/reader/{chapter_id}/resources"
    kavita_base = (cfg.kavita_url or "").rstrip("/")
    # Kavita uses protocol-relative URLs: //host:port/api/ - extract host part
    kavita_host = re.sub(r"^https?://", "", kavita_base) if kavita_base else ""

    def repl(m: re.Match) -> str:
        return f"{resource_url}?file={m.group(1)}"

    # Override <base href> so relative URLs resolve to our app, not Kavita (prevents
    # resources from loading via Kavita's host, which causes ERR_SSL_PROTOCOL_ERROR)
    html = re.sub(
        r"<base\s+[^>]*href\s*=\s*[\"'][^\"']*[\"'][^>]*/?\s*>",
        f'<base href="{app_base}/">',
        html,
        flags=re.I,
    )
    if "<base" not in html.lower():
        html = re.sub(r"<head(?:\s[^>]*)?>", lambda m: m.group(0) + f'<base href="{app_base}/">', html, count=1, flags=re.I)

    # Replace protocol-relative Kavita URLs (//host:port/api/Book/82/book-resources?file=...)
    if kavita_host:
        html = re.sub(
            rf"//{re.escape(kavita_host)}/api/[Bb]ook/\d+/book-resources\?file=([^\"'&]+)",
            repl,
            html,
            flags=re.I,
        )
    # Replace absolute Kavita URLs (http(s)://host/api/Book/...)
    if kavita_base:
        html = re.sub(
            rf"{re.escape(kavita_base)}/api/[Bb]ook/\d+/book-resources\?file=([^\"'&]+)",
            repl,
            html,
            flags=re.I,
        )
    # Replace path-only URLs
    html = re.sub(
        r"/api/[Bb]ook/\d+/book-resources\?file=([^\"'&]+)",
        repl,
        html,
        flags=re.I,
    )
    # Replace relative resource URLs (e.g. resources?file=cover.jpeg, ./book-resources?file=...)
    # Use fixed-width negative lookbehind to avoid re-matching inside already-rewritten URLs (prevents doubling)
    # Python re requires fixed-width lookbehind, so we use the actual chapter_id
    html = re.sub(
        rf"(?<!reader/{chapter_id}/)(?:\./)?(?:book-)?resources\?file=([^\"'&]+)",
        repl,
        html,
        flags=re.I,
    )
    # Replace Book/82/book-resources?file=... (path relative to api/)
    html = re.sub(
        r"[Bb]ook/\d+/book-resources\?file=([^\"'&]+)",
        repl,
        html,
        flags=re.I,
    )
    # Rewrite all img src with relative paths (cover.jpeg, Images/cover.jpg, ../OEBPS/Images/x.jpg)
    from urllib.parse import quote

    def _rewrite_img(m: re.Match) -> str:
        path = m.group(2).replace("\\", "/").strip()
        # Normalize: collapse ../ and ./
        parts = []
        for p in path.split("/"):
            if p == "..":
                if parts:
                    parts.pop()
            elif p and p != ".":
                parts.append(p)
        file_path = "/".join(parts) if parts else path
        return f'<img {m.group(1)}src="{resource_url}?file={quote(file_path, safe="/")}"'

    html = re.sub(
        r'<img\s+([^>]*?)src\s*=\s*["\'](?!https?://|//|data:)([^"\']+)["\']',
        _rewrite_img,
        html,
        flags=re.I,
    )
    return HTMLResponse(html)


@router.get("/reader/{chapter_id}/resources")
async def reader_book_resources(
    chapter_id: int,
    file: str = Query(..., description="Path to resource within EPUB"),
):
    """Proxy a resource (image, font) from within an EPUB. No auth required so img tags in
    rendered EPUB HTML can load images (they don't send Bearer tokens)."""
    content = await kavita.get_book_resources(chapter_id, file)
    if not content:
        raise HTTPException(status_code=404, detail="Resource not found")
    ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""
    mime = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
        "woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf", "otf": "font/otf",
    }.get(ext, "application/octet-stream")
    return Response(content=content, media_type=mime)


@router.get("/ebook-match")
async def ebook_match_by_title(
    title: str = Query(..., min_length=1),
    author: str = Query(""),
    seriesName: str = Query(""),
    seriesIndex: str = Query(""),
    _user: User = Depends(get_current_user),
):
    """Resolve a catalog title to the correct Kavita ebook chapter (series-aware)."""
    if not title.strip():
        return {"chapterId": None}
    match = await kavita_ebook_match.resolve_kavita_ebook(
        title=title,
        author=author,
        series_name=seriesName or None,
        series_index=seriesIndex or None,
    )
    if not match:
        return {"chapterId": None}
    return match


def _title_matches(q: str, name: str) -> bool:
    """Same fuzzy match logic as ebook_match."""
    q_lower = q.lower().strip()
    n = name.lower().strip()
    if not q_lower or not n:
        return False
    return n == q_lower or q_lower in n or (len(q_lower) > 3 and n in q_lower)


@router.post("/format-matches")
async def format_matches_batch(
    body: FormatMatchesRequest,
    _user: User = Depends(get_current_user),
):
    """Return hasEbook/hasAudio for each title (for library card icons)."""
    titles = [t.strip() for t in body.titles if t and t.strip()]
    if not titles:
        return {}

    ebook_titles: set[str] = set()
    abs_titles: set[str] = set()

    # Kavita: match input titles against series names (no volume fetch needed for hasEbook)
    try:
        series_list = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
        for s in series_list:
            name = (s.get("name") or s.get("localizedName") or s.get("originalName") or "").strip()
            if not name:
                continue
            for t in titles:
                if _title_matches(t, name):
                    ebook_titles.add(t)
    except Exception as e:
        logger.warning("Format matches Kavita: %s", e)

    # ABS: match input titles against item titles
    try:
        abs_items = await audiobookshelf.get_all_items()
        for item in abs_items:
            name = (item.get("title") or "").strip()
            if not name:
                continue
            for t in titles:
                if _title_matches(t, name):
                    abs_titles.add(t)
    except Exception as e:
        logger.warning("Format matches ABS: %s", e)

    result: dict[str, dict] = {}
    for t in titles:
        result[t] = {
            "hasEbook": t in ebook_titles,
            "hasAudio": t in abs_titles,
        }
    return result


@router.get("/check/{google_volume_id:path}")
async def check_in_library(
    google_volume_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StreamingLibraryItem).where(
            and_(
                StreamingLibraryItem.user_id == user.id,
                StreamingLibraryItem.google_volume_id == google_volume_id,
            )
        )
    )
    item = result.scalar_one_or_none()
    if item:
        return {"inLibrary": True, "item": _serialize(item)}
    return {"inLibrary": False, "item": None}


def _norm_title_key(s: str) -> str:
    """Normalize titles for private-hide / in-library matching."""
    s = (s or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _titles_overlap(a: str, b: str) -> bool:
    """True when normalized titles are equal or one substantial title contains the other."""
    ka, kb = _norm_title_key(a), _norm_title_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    # Avoid tiny false positives ("it", "a", "war")
    return len(shorter) >= 10 and shorter in longer


async def _get_private_titles_for_others(current_user_id: int, db: AsyncSession) -> set[str]:
    """Normalized title keys of OTHER users' private downloads to hide.

    Never includes titles the current user also requested (so you always see
    your own private books even if someone else privately requested the same title).
    """
    others = (
        await db.execute(
            select(DownloadRequest.title).where(
                DownloadRequest.is_private == True,  # noqa: E712
                DownloadRequest.user_id != current_user_id,
                DownloadRequest.status != "failed",
            )
        )
    ).scalars().all()
    mine = (
        await db.execute(
            select(DownloadRequest.title).where(
                DownloadRequest.user_id == current_user_id,
                DownloadRequest.status != "failed",
            )
        )
    ).scalars().all()

    other_keys = {_norm_title_key(t) for t in others if t}
    my_keys = {_norm_title_key(t) for t in mine if t}
    # Drop keys that overlap the viewer's own requests
    hidden: set[str] = set()
    for ok in other_keys:
        if not ok:
            continue
        if any(_titles_overlap(ok, mk) for mk in my_keys if mk):
            continue
        hidden.add(ok)
    return hidden


def _is_hidden(title: str, hidden_titles: set[str]) -> bool:
    """hidden_titles is a set of normalized keys (see _get_private_titles_for_others)."""
    if not hidden_titles or not title:
        return False
    key = _norm_title_key(title)
    if not key:
        return False
    if key in hidden_titles:
        return True
    return any(_titles_overlap(key, h) for h in hidden_titles)


@router.get("/in-library-global")
async def check_in_library_global(
    title: str = Query(..., min_length=1),
    author: str = Query(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if ANY user has this book in the library (for 'already in library' indicator).

    Includes private downloads (title only — never who requested them) so
    duplicate requests are discouraged even in private mode.
    """
    _ = user  # auth required
    _ = author  # reserved for future author-aware matching
    q_key = _norm_title_key(title)
    if not q_key:
        return {"inLibrary": False}

    result = await db.execute(
        select(DownloadRequest.title).where(DownloadRequest.status != "failed")
    )
    for req_title in result.scalars().all():
        if req_title and _titles_overlap(title, req_title):
            return {"inLibrary": True}

    try:
        abs_items = await audiobookshelf.get_all_items()
        for item in abs_items:
            if _titles_overlap(title, item.get("title", "")) or _title_matches(
                title, item.get("title", "")
            ):
                return {"inLibrary": True}
    except Exception:
        pass

    try:
        kavita_series = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
        for s in kavita_series:
            name = s.get("name") or s.get("localizedName") or s.get("originalName") or ""
            if _titles_overlap(title, name) or _title_matches(title, name):
                return {"inLibrary": True}
    except Exception:
        pass

    return {"inLibrary": False}


async def _get_user_item(item_id: int, user_id: int, db: AsyncSession) -> StreamingLibraryItem:
    result = await db.execute(
        select(StreamingLibraryItem).where(
            and_(StreamingLibraryItem.id == item_id, StreamingLibraryItem.user_id == user_id)
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


async def _lookup_cover_for_volume(volume_id: str, title: str, author: str) -> str:
    """Best-effort cover for Personal Collection rows missing artwork."""
    return await google_books.lookup_cover_url(volume_id, title, author)


def _serialize(item: StreamingLibraryItem) -> dict:
    tracks = _get_tracks(item)
    return {
        "id": item.id,
        "googleVolumeId": item.google_volume_id,
        "title": item.title,
        "author": item.author,
        "coverUrl": item.cover_url,
        "genre": getattr(item, "genre", "") or "",
        "magnetLink": item.magnet_link or "",
        "streamStatus": item.stream_status,
        "progressSeconds": item.progress_seconds,
        "totalSeconds": item.total_seconds,
        "tracks": tracks,
        "createdAt": item.created_at.isoformat() if item.created_at else "",
        "updatedAt": item.updated_at.isoformat() if item.updated_at else "",
    }
