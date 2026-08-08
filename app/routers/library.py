"""User streaming library: curated collection of books with RD streaming."""

import hashlib
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi.responses import FileResponse, HTMLResponse, Response

from app.database import get_db, async_session
from app.models import User, StreamingLibraryItem, DownloadRequest
from app.utils.auth import get_current_user
from app.services import debrid, debrid_tokens, audiobookshelf, kavita, google_books, hardcover
from app.services import kavita_ebook_match
from app.services import library_collection_cache
from app.services.google_books import GENRE_TAXONOMY
from app.utils.book_series import (
    is_junk_library_label,
    is_junk_series_hint,
    library_series_from_title,
    parse_abs_series_label,
)
from app.routers.stream import tracks_with_stable_urls

logger = logging.getLogger(__name__)

# Coalesce concurrent full collection rebuilds (refresh=true storms on Pi).
_collection_inflight: dict[str, asyncio.Future] = {}

# Build a lookup: lowercased sub-genre name/keyword -> top-level genre name
# Same taxonomy as store `/books/genres` (GENRE_TAXONOMY).
_GENRE_TO_TOPLEVEL: dict[str, str] = {}
_TAXONOMY_TOP_NAMES: set[str] = set()


async def _singleflight(key: str, factory):
    """Share one in-flight coroutine result across concurrent callers."""
    existing = _collection_inflight.get(key)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _collection_inflight[key] = fut
    try:
        result = await factory()
        if not fut.done():
            fut.set_result(result)
        return result
    except BaseException as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if _collection_inflight.get(key) is fut:
            _collection_inflight.pop(key, None)


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
    """Optionally fill *empty* genres from Hardcover (taxonomy-mapped).

    Author, series, sequence, and any existing local genres stay on ABS/Kavita/PC
    metadata — never overwritten. HC genre taxonomy is often wrong (Farseer→Romance,
    DCC→Comedy), so it only fills when local genres normalized to nothing.
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
            # Prefer already-normalized local genres; junk-only (Audiobook) → empty.
            local_genres = _normalize_item_genres(item.get("genres") or [])
            if not local_genres:
                singular = (item.get("genre") or "").strip()
                mapped = _map_to_toplevel(singular) if singular else None
                if mapped:
                    local_genres = [mapped]
            if local_genres:
                # Good local genres — never overwrite with Hardcover taxonomy.
                if local_genres != (item.get("genres") or []):
                    out = {**item, "genres": local_genres}
                    if "genre" in item:
                        out["genre"] = local_genres[0]
                    return out
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
        "playbackRate": task.get("playback_rate"),
        "updatedAt": task.get("updated_at"),
    }


# Chapter-style folder basenames that ABS indexes as separate library items when
# a multi-file book was extracted into sibling folders (Red Seas Under Red Skies).
_ABS_CHAPTER_FOLDER_RE = re.compile(
    r"(?i)^(?:"
    r"ch(?:apter)?[\s._-]*\d+"
    r"|prologue\b"
    r"|epilogue\b"
    r"|reminiscence[\s._-]*\d*"
    r"|cards?\s+(?:in|on)\s+the\b"
    r"|.*\bintro\b"
    r")"
)


def _abs_folder_basename(item: dict) -> str:
    rel = str(item.get("relPath") or item.get("path") or "").replace("\\", "/").rstrip("/")
    return rel.split("/")[-1].strip() if rel else ""


def _abs_item_score(item: dict) -> tuple[int, int, int]:
    """Prefer complete books over single-track chapter fragments."""
    return (
        int(item.get("numTracks") or 0),
        int(item.get("duration") or 0),
        1 if str(item.get("asin") or "").strip() else 0,
    )


def _dedupe_abs_shelf_items(items: list[dict]) -> list[dict]:
    """Collapse duplicate ABS rows so the shelf counts unique books.

    Two inflation sources observed live:
    1. Same ASIN imported under two folder trees (Assassin's Quest, etc.)
    2. One book split into many chapter folders that all share the book title
       (26× \"Red Seas Under Red Skies\" single-track items).
    """
    from collections import defaultdict

    from app.services.forge_pipeline import normalize_asin

    # Pass 1: unique by ASIN (keep richest item).
    by_asin: dict[str, dict] = {}
    remainder: list[dict] = []
    for item in items:
        asin = normalize_asin(item.get("asin"))
        if not asin:
            remainder.append(item)
            continue
        prev = by_asin.get(asin)
        if prev is None or _abs_item_score(item) > _abs_item_score(prev):
            by_asin[asin] = item

    candidates = list(by_asin.values()) + remainder

    # Pass 2: title+author groups — drop chapter-folder fragments.
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in candidates:
        key = (
            str(item.get("title") or "").strip().casefold(),
            str(item.get("author") or "").strip().casefold(),
        )
        groups[key].append(item)

    out: list[dict] = []
    for (title, _author), group in groups.items():
        if len(group) == 1:
            out.extend(group)
            continue

        canonical: list[dict] = []
        fragments: list[dict] = []
        for item in group:
            base = _abs_folder_basename(item).casefold()
            tracks = int(item.get("numTracks") or 0)
            dur = int(item.get("duration") or 0)
            looks_chapter = bool(base and _ABS_CHAPTER_FOLDER_RE.search(base))
            short_single = tracks <= 1 and 0 < dur < 8 * 3600
            folder_is_title = bool(base and title and (base == title or title in base or base in title))
            if looks_chapter or (short_single and len(group) >= 3 and not folder_is_title):
                fragments.append(item)
            else:
                canonical.append(item)

        if canonical and fragments:
            # Keep real book folder(s); drop chapter siblings.
            if len(canonical) == 1:
                out.extend(canonical)
            else:
                # Multiple non-fragment copies (path twins without ASIN) — keep best.
                out.append(max(canonical, key=_abs_item_score))
            continue

        if len(group) >= 3 and len(fragments) >= len(group) - 1:
            out.append(max(group, key=_abs_item_score))
            continue

        if len(group) == 2 and all(int(it.get("duration") or 0) > 10_000 for it in group):
            # Likely full-book path duplicate without a shared ASIN.
            out.append(max(group, key=_abs_item_score))
            continue

        out.extend(group)

    return out


@router.get("/abs/collection")
async def abs_collection(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    refresh: bool = Query(
        False,
        description="Bypass short-TTL collection + ABS item caches (use after scans).",
    ),
):
    """Return all ABS library items grouped by store top-level genres."""
    cache_key = f"abs_coll:{user.id}"
    if not refresh:
        cached = library_collection_cache.get(cache_key)
        if cached is not None:
            return cached

    async def _build() -> dict:
        # Re-check TTL cache inside singleflight (first waiter may have filled it).
        if not refresh:
            hit = library_collection_cache.get(cache_key)
            if hit is not None:
                return hit

        # Stale-while-scanning: don't rebuild from ABS mid-scan (partial items).
        from app.services import library_refresh as refresh_pipeline

        if refresh_pipeline.is_running():
            stale = library_collection_cache.get_stale(cache_key)
            if stale is not None:
                return stale
            # No prior payload - avoid assembling a mid-scan partial shelf.
            return {"items": [], "totalItems": 0, "scanning": True}

        hidden_titles = await _get_private_titles_for_others(user.id, db)
        raw_items = [
            it for it in await audiobookshelf.get_all_items(force_refresh=refresh)
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
        # Collapse ASIN twins + chapter-folder fragments (e.g. 26× Red Seas parts)
        # so the shelf count tracks unique books (~189) not raw ABS rows (~217).
        cleaned = _dedupe_abs_shelf_items(cleaned)
        # Skip HC enrich on forced refresh — it fans out 8 concurrent lookups and
        # routinely times out on ~200 items, burning Pi CPU for no genre gain.
        if refresh:
            items = cleaned
        else:
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
        # Unique books only — multi-genre titles appear in several buckets but must
        # not inflate the My Library "N audiobooks" subtitle.
        unique_count = len(items)
        sorted_genres = dict(sorted(genres.items(), key=lambda x: x[0]))
        for bucket in sorted_genres.values():
            bucket.sort(key=lambda x: x.get("addedAt") or 0, reverse=True)
        ungrouped.sort(key=lambda x: x.get("addedAt") or 0, reverse=True)
        payload = {
            "genres": sorted_genres,
            "ungrouped": ungrouped,
            "totalItems": unique_count,
        }
        library_collection_cache.set(cache_key, payload)
        return payload

    return await _singleflight(f"abs_coll_build:{user.id}:{refresh}", _build)


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
    """Full metadata for one ABS item — powers the library book detail page.

    Author / series / sequence / genres come from ABS (file / LibraForge) first.
    Hardcover may fill only empty genres — never replaces local series or author.
    """
    item = await audiobookshelf.get_library_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    # Reuse collection normalizer so seriesName wins over mismatched series[].
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
            s for s in (normalized.get("series") or [])
            if (s.get("name") or "").strip() and not is_junk_series_hint(s.get("name") or "")
        ]

    genres = abs_genres
    if not genres:
        try:
            hc = await hardcover.match_library_book(
                title=title, author=author, series_hint=sname,
            )
            hc_genres = _normalize_item_genres((hc or {}).get("genres") or [])
            if hc_genres:
                genres = hc_genres
        except Exception:
            logger.debug("Hardcover match failed for ABS item %s", item_id, exc_info=True)

    raw_meta = (item.get("media") or {}).get("metadata") or {}
    return {
        "itemId": item.get("id", "") or normalized.get("itemId", ""),
        "title": title,
        "subtitle": normalized.get("subtitle") or "",
        "author": author,
        "narrator": normalized.get("narrator") or "",
        "description": normalized.get("description") or "",
        "publisher": raw_meta.get("publisher") or "",
        "publishedYear": raw_meta.get("publishedYear") or "",
        "genres": genres,
        "series": out_series,
        "seriesName": sname,
        "sequence": seq,
        "asin": normalized.get("asin") or "",
        "duration": normalized.get("duration") or 0,
        "numTracks": normalized.get("numTracks") or 0,
        "coverUrl": f"/api/stream/abs/proxy/cover/{item_id}",
    }


def _kavita_file_stem(file_entry: dict) -> str:
    path = str(file_entry.get("filePath") or file_entry.get("fileName") or "")
    return Path(path).stem if path else ""


def _kavita_file_name(file_entry: dict) -> str:
    path = str(file_entry.get("filePath") or file_entry.get("fileName") or "")
    return Path(path).name if path else ""


def _kavita_file_key(file_entry: dict) -> str:
    """Stable per-file identity when Kavita collapses multiple volumes into one chapter."""
    path = str(file_entry.get("filePath") or file_entry.get("fileName") or "").strip()
    if not path:
        return ""
    # Prefer basename — local mounts remount under different prefixes.
    return Path(path).name.lower()


def _ebook_cover_url(
    series_id: int | None,
    *,
    volume_id: int | None = None,
    chapter_id: int | None = None,
) -> str:
    """Build the proxy cover URL for a Kavita ebook (series → volume → chapter)."""
    if not series_id:
        return ""
    cover_url = f"/api/library/reader/cover/ebook?seriesId={series_id}"
    if volume_id:
        cover_url += f"&volumeId={volume_id}"
    if chapter_id:
        cover_url += f"&chapterId={chapter_id}"
    return cover_url


def _kavita_local_file_path(file_entry: dict | None) -> Path | None:
    """Resolve an on-disk ebook path from a Kavita file entry."""
    if not file_entry:
        return None
    raw = str(file_entry.get("filePath") or file_entry.get("fileName") or "").strip()
    if not raw:
        return None
    from app.services.kavita import _kavita_path_to_local

    local = _kavita_path_to_local(raw)
    if local is not None:
        return local
    # Basename-only fallback: search is too expensive; return Path for sidecar parent guess.
    p = Path(raw)
    return p if p.is_file() else None


def _ebook_override_for_file_entry(file_entry: dict | None) -> dict | None:
    """Load file-scoped ebook_applied.json override for a Kavita file entry."""
    from app.services.ebook_quick_review import load_applied_ebook_override

    local = _kavita_local_file_path(file_entry)
    if local is None:
        return None
    return load_applied_ebook_override(local)


def _kavita_volume_title(series_name: str, vol: dict, chapter: dict, file_entry: dict | None = None) -> str:
    """Best display title for one Kavita volume (prefer file stem over placeholder chapter titles)."""
    if file_entry is not None:
        stem = _kavita_file_stem(file_entry)
        if stem:
            return stem
    files = chapter.get("files") or []
    best_stem = ""
    best_rank = -1
    for f in files:
        stem = _kavita_file_stem(f)
        if not stem:
            continue
        # Prefer canonical file over re-download duplicates: "Title (2).epub"
        rank = 0 if re.search(r"\s\(\d+\)$", stem) else 10
        if rank > best_rank:
            best_rank = rank
            best_stem = stem
    if best_stem:
        return best_stem
    for key in ("titleName", "title", "range"):
        raw = (chapter.get(key) or vol.get(key) or "").strip()
        if raw and raw not in ("-100000", "0", "Special"):
            return raw
    vol_num = vol.get("number")
    if vol_num is not None and float(vol_num or 0) > 0:
        return f"{series_name} {vol_num}".strip()
    return series_name


def _kavita_chapter_file_entries(vol: dict, chapter: dict) -> list[dict]:
    """Return one logical volume per distinct ebook file.

    When Kavita merges sibling books into a single chapter (identical series-index
    after a bad metadata stamp), the shelf must still show one card per file.
    """
    files = [f for f in (chapter.get("files") or []) if _kavita_file_key(f)]
    if not files:
        return [{}]
    # Drop obvious re-download duplicates ("Title (2).epub") when a canonical twin exists.
    stems = {_kavita_file_stem(f) for f in files}
    filtered: list[dict] = []
    for f in files:
        stem = _kavita_file_stem(f)
        if re.search(r"\s\(\d+\)$", stem):
            base = re.sub(r"\s\(\d+\)$", "", stem)
            if base in stems or any(s == base for s in stems):
                continue
        filtered.append(f)
    return filtered or files


def _kavita_collection_items_from_series(
    s: dict,
    volumes: list,
    meta: dict,
    *,
    hidden_titles: set[str],
) -> list[dict]:
    """Expand one Kavita series into one shelf item per volume/file so series books accumulate."""
    name = s.get("name") or s.get("localizedName") or s.get("originalName") or ""
    if not name or _is_hidden(name, hidden_titles):
        return []

    writers = meta.get("writers") or s.get("authors") or []
    author = ""
    if writers:
        author = (writers[0] or {}).get("name", "") if isinstance(writers[0], dict) else str(writers[0])
    genres = _normalize_item_genres(meta.get("genres") or [])

    added_at = s.get("created") or s.get("lastChapterAdded") or meta.get("releaseYear") or 0
    try:
        if isinstance(added_at, str) and added_at:
            from datetime import datetime

            added_ms = int(datetime.fromisoformat(added_at.replace("Z", "+00:00")).timestamp() * 1000)
        else:
            added_ms = int(added_at or 0)
            if added_ms < 10_000_000_000:
                added_ms = added_ms * 1000 if added_ms else 0
    except Exception:
        added_ms = 0

    sid = s.get("id")
    # (sort_key, vol, ch, file_entry|None)
    vol_entries: list[tuple[float, dict, dict, dict | None]] = []
    for vol in volumes or []:
        chapters = vol.get("chapters") or []
        if not chapters:
            continue
        ch = chapters[0]
        if ch.get("id") is None:
            continue
        file_entries = _kavita_chapter_file_entries(vol, ch)
        for f in file_entries:
            stem = _kavita_file_stem(f) if f else ""
            file_num = kavita_ebook_match._book_number_from_text(stem) if stem else None
            vol_num = file_num if file_num is not None else kavita_ebook_match._volume_index(vol)
            sort_key = vol_num if vol_num is not None else 999.0
            vol_entries.append((sort_key, vol, ch, f or None))

    if not vol_entries:
        # Series row with no volumes yet — keep a placeholder card.
        return [{
            "seriesId": sid,
            "volumeId": None,
            "volumeNumber": None,
            "title": name,
            "author": author,
            "coverUrl": _ebook_cover_url(sid),
            "chapterId": None,
            "fileKey": None,
            "fileName": None,
            "genres": genres,
            "seriesName": "",
            "sequence": "",
            "series": [],
            "addedAt": added_ms,
            "volumeCount": 0,
            "source": "kavita",
        }]

    vol_entries.sort(key=lambda x: (x[0], (_kavita_file_stem(x[3]) if x[3] else "")))
    multi = len(vol_entries) > 1
    items: list[dict] = []
    for sort_key, vol, ch, file_entry in vol_entries:
        chapter_id = int(ch["id"])
        volume_id = vol.get("id")
        stem = _kavita_file_stem(file_entry) if file_entry else ""
        file_num = kavita_ebook_match._book_number_from_text(stem) if stem else None
        vol_num = file_num if file_num is not None else kavita_ebook_match._volume_index(vol)
        title = _kavita_volume_title(name, vol, ch, file_entry) if multi else name
        seq = ""
        if vol_num is not None and vol_num > 0:
            seq = str(int(vol_num)) if float(vol_num).is_integer() else str(vol_num)
        sname = name if multi else ""
        if not sname:
            inferred = library_series_from_title(title)
            if inferred and not is_junk_series_hint(inferred[0]):
                sname, seq = inferred[0], str(inferred[1] or seq)

        cover_url = _ebook_cover_url(sid, volume_id=volume_id, chapter_id=chapter_id)
        file_key = _kavita_file_key(file_entry) if file_entry else None
        file_name = _kavita_file_name(file_entry) if file_entry else None

        item = {
            "seriesId": sid,
            "volumeId": volume_id,
            "volumeNumber": vol_num,
            "title": title,
            "author": author,
            "coverUrl": cover_url,
            "chapterId": chapter_id,
            "fileKey": file_key,
            "fileName": file_name,
            "genres": genres,
            "seriesName": sname,
            "sequence": seq,
            "series": [{"name": sname, "sequence": seq}] if sname else [],
            "addedAt": added_ms,
            "volumeCount": len(vol_entries),
            "source": "kavita",
        }
        # Prefer manually saved / pipeline sidecar over live Kavita (ABS pattern).
        from app.services.ebook_quick_review import apply_ebook_override_fields

        apply_ebook_override_fields(
            item,
            _ebook_override_for_file_entry(file_entry),
            multi_volume=multi,
        )
        items.append(item)
    return items


async def kavita_ebook_inventory(*, force_refresh: bool = False) -> dict[str, int]:
    """Series + shelf ebook counts using the same expansion as /library/kavita/collection.

    Admin Health previously reported ebook *series* while My Library reports expanded
    volume/file shelf cards — this helper keeps those scopes explicit and aligned.
    """
    all_series = await kavita.get_all_series(force_refresh=force_refresh)
    ebook_series = [s for s in all_series if s.get("format") in kavita.EBOOK_FORMATS]
    sem = asyncio.Semaphore(3)

    async def _shelf_len(s: dict) -> int:
        sid = s.get("id", 0)
        async with sem:
            volumes = await kavita.get_series_volumes(sid)
        return len(
            _kavita_collection_items_from_series(s, volumes, {}, hidden_titles=set())
        )

    shelf_lens = await asyncio.gather(*[_shelf_len(s) for s in ebook_series]) if ebook_series else []
    return {
        "series_count": len(all_series),
        "ebook_series_count": len(ebook_series),
        # Same unit as My Library `totalItems` after library refresh.
        "ebook_count": int(sum(shelf_lens)),
    }


@router.get("/kavita/collection")
async def kavita_collection(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    refresh: bool = Query(
        False,
        description="Bypass short-TTL collection + Kavita series caches (use after scans).",
    ),
):
    """Return Kavita ebook volumes (EPUB/PDF) for the library view.

    Multi-volume series expand to one shelf item per volume so Book 1/2/3 all appear.
    """
    cache_key = f"kavita_coll:{user.id}"
    if not refresh:
        cached = library_collection_cache.get(cache_key)
        if cached is not None:
            return cached

    async def _build() -> dict:
        if not refresh:
            hit = library_collection_cache.get(cache_key)
            if hit is not None:
                return hit

        # Stale-while-scanning: while the refresh pipeline runs, Kavita is
        # rebuilding series/volumes and a rebuild would snapshot partial data
        # (books vanish, multi-volume series collapse). Serve the last
        # assembled payload instead; the post-scan refresh rebuilds fresh.
        from app.services import library_refresh as refresh_pipeline

        if refresh_pipeline.is_running():
            stale = library_collection_cache.get_stale(cache_key)
            if stale is not None:
                return stale

        hidden_titles = await _get_private_titles_for_others(user.id, db)
        series = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS, force_refresh=refresh)
        # Limit fan-out: each series hits volumes+metadata; 10 concurrent melted the Pi.
        sem = asyncio.Semaphore(3)

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
            items.extend(
                _kavita_collection_items_from_series(
                    s, volumes, meta, hidden_titles=hidden_titles
                )
            )
        # Skip HC enrich on forced refresh (same Pi load reason as ABS collection).
        if not refresh:
            items = await _enrich_items_via_hardcover(items)
        # Newest first by default for the All shelf
        items.sort(key=lambda x: x.get("addedAt") or 0, reverse=True)
        payload = {"items": items, "totalItems": len(items)}
        library_collection_cache.set(cache_key, payload)
        return payload

    return await _singleflight(f"kavita_coll_build:{user.id}:{refresh}", _build)


@router.get("/kavita/item/{series_id}")
async def kavita_item_detail(
    series_id: int,
    _user: User = Depends(get_current_user),
):
    """Full metadata for one Kavita ebook series — library ebook detail page."""
    series_list = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
    series = next((s for s in series_list if s.get("id") == series_id), None)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    name = series.get("name") or series.get("localizedName") or series.get("originalName") or ""
    volumes, meta = await asyncio.gather(
        kavita.get_series_volumes(series_id),
        kavita.get_series_metadata(series_id),
    )
    meta = meta or {}
    writers = meta.get("writers") or series.get("authors") or []
    author = ""
    if writers:
        author = (writers[0] or {}).get("name", "") if isinstance(writers[0], dict) else str(writers[0])
    book_num = kavita_ebook_match._book_number_from_text(name)
    chapter_id = kavita_ebook_match._pick_chapter_id(volumes, book_num)
    volume_id: int | None = None
    volume_list: list[dict] = []
    for vol in volumes or []:
        chapters = vol.get("chapters") or []
        if not chapters:
            continue
        ch = chapters[0]
        ch_id = ch.get("id")
        if ch_id is None:
            continue
        v_id = vol.get("id")
        for file_entry in _kavita_chapter_file_entries(vol, ch):
            stem = _kavita_file_stem(file_entry) if file_entry else ""
            file_num = kavita_ebook_match._book_number_from_text(stem) if stem else None
            v_num = file_num if file_num is not None else kavita_ebook_match._volume_index(vol)
            v_title = _kavita_volume_title(name, vol, ch, file_entry or None)
            seq = ""
            if v_num is not None and v_num > 0:
                seq = str(int(v_num)) if float(v_num).is_integer() else str(v_num)
            ch_summary = (
                (ch.get("summary") or vol.get("summary") or ch.get("description") or "")
                .strip()
            )
            vol_row = {
                "volumeId": v_id,
                "volumeNumber": v_num,
                "chapterId": int(ch_id),
                "title": v_title,
                "author": author,
                "description": ch_summary or None,
                "coverUrl": _ebook_cover_url(
                    series_id, volume_id=v_id, chapter_id=int(ch_id)
                ),
                "fileKey": _kavita_file_key(file_entry) if file_entry else None,
                "fileName": _kavita_file_name(file_entry) if file_entry else None,
                "seriesName": "",
                "sequence": seq,
            }
            from app.services.ebook_quick_review import apply_ebook_override_fields

            ov = _ebook_override_for_file_entry(file_entry)
            apply_ebook_override_fields(vol_row, ov, multi_volume=True)
            if ov and ov.get("summary"):
                vol_row["description"] = ov["summary"]
            volume_list.append(vol_row)
            if chapter_id is not None and int(ch_id) == int(chapter_id):
                volume_id = v_id
            elif chapter_id is None:
                chapter_id = int(ch_id)
                volume_id = v_id
    volume_list.sort(
        key=lambda v: (
            v["volumeNumber"] is None,
            v["volumeNumber"] if v["volumeNumber"] is not None else 999.0,
            v.get("title") or "",
        )
    )
    multi = len(volume_list) > 1
    for vol_row in volume_list:
        if vol_row.get("seriesName"):
            continue
        if multi:
            vol_row["seriesName"] = name
            continue
        inferred_one = library_series_from_title(vol_row.get("title") or name)
        if inferred_one and not is_junk_series_hint(inferred_one[0]):
            vol_row["seriesName"] = inferred_one[0]
            if not vol_row.get("sequence"):
                vol_row["sequence"] = str(inferred_one[1] or "")
    cover_url = _ebook_cover_url(series_id, volume_id=volume_id, chapter_id=chapter_id)
    genres = _normalize_item_genres(meta.get("genres") or [])
    description = (meta.get("summary") or meta.get("description") or "").strip()
    inferred = library_series_from_title(name)
    sname, seq = "", ""
    if inferred and not is_junk_series_hint(inferred[0]):
        sname, seq = inferred[0], str(inferred[1] or "")
    if not sname and multi:
        sname = name
    series_out = [{"name": sname, "sequence": seq}] if sname else []

    abs_item_id = ""
    try:
        for item in await audiobookshelf.get_all_items():
            abs_title = (item.get("title") or "").strip()
            if abs_title and _title_matches(name, abs_title):
                abs_item_id = item.get("itemId") or ""
                break
    except Exception:
        logger.debug("ABS match for Kavita series %s failed", series_id, exc_info=True)

    # Prefer file-scoped override on the active/first volume for detail header fields.
    # Client re-selects by ?chapter=&file= — volumes[] carry per-volume cover/synopsis.
    active = None
    if chapter_id is not None:
        active = next((v for v in volume_list if v.get("chapterId") == int(chapter_id)), None)
    if active is None and volume_list:
        active = volume_list[0]
    detail_title = name
    if active and active.get("metadataOverride") and active.get("title"):
        # Multi-volume: keep series name as header only when volumes disagree; else use override.
        if len(volume_list) <= 1:
            detail_title = active["title"]
        else:
            detail_title = name
    if active and active.get("author"):
        author = active["author"]
    if active and active.get("description"):
        description = active["description"]
    if active and active.get("coverUrl"):
        cover_url = active["coverUrl"]
    if active and active.get("seriesName"):
        sname = active["seriesName"]
        seq = str(active.get("sequence") or seq or "")
        series_out = [{"name": sname, "sequence": seq}] if sname else series_out

    return {
        "seriesId": series_id,
        "title": detail_title,
        "author": author,
        "description": description,
        "genres": genres,
        "chapterId": chapter_id,
        "coverUrl": cover_url,
        "series": series_out,
        "volumes": volume_list,
        "absItemId": abs_item_id or None,
    }



def _search_tokens(q: str) -> list[str]:
    """Split a library search query into casefolded tokens (drop empties)."""
    return [t for t in re.split(r"\s+", (q or "").strip().casefold()) if t]


def _metadata_search_blob(item: dict) -> str:
    """Join searchable library metadata fields into one casefolded haystack."""
    parts: list[str] = []
    for key in (
        "title", "subtitle", "author", "narrator", "seriesName", "asin", "description",
        "genre",
    ):
        val = item.get(key)
        if val:
            parts.append(str(val))
    for g in item.get("genres") or []:
        if isinstance(g, str) and g.strip():
            parts.append(g)
        elif isinstance(g, dict):
            label = g.get("name") or g.get("title") or g.get("tag") or ""
            if label:
                parts.append(str(label))
    for s in item.get("series") or []:
        if isinstance(s, dict) and s.get("name"):
            parts.append(str(s["name"]))
        elif isinstance(s, str) and s.strip():
            parts.append(s)
    # Snippet-sized description only (avoid huge payloads dominating match cost).
    return " ".join(parts).casefold()


def _tokens_match_metadata(item: dict, tokens: list[str]) -> bool:
    """True when every query token appears somewhere in the item's metadata blob."""
    if not tokens:
        return False
    blob = _metadata_search_blob(item)
    return all(t in blob for t in tokens)


@router.get("/search")
async def search_library_unified(
    q: str = Query("", min_length=1),
    media: str = Query("all", description="Filter: all, audiobooks, ebooks"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search across ABS, Kavita ebooks, and user's RD streaming library.

    Matches tokenized query against title, author, series, narrator, genres,
    ASIN, subtitle, and description snippets (case-insensitive).
    """
    hidden_titles = await _get_private_titles_for_others(user.id, db)
    results = []
    seen_abs_ids: set[str] = set()
    seen_kavita_ids: set[int] = set()
    tokens = _search_tokens(q)

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

        # Enrich from warm ABS cache only — never block search on a full library pull
        # (that was ~20s cold). Genre/narrator/ASIN matches still work when cache is warm.
        cached_abs = audiobookshelf.peek_cached_all_items()
        if cached_abs:
            for item in cached_abs:
                iid = item.get("itemId", "")
                if iid in seen_abs_ids:
                    continue
                title = item.get("title", "")
                if _is_hidden(title, hidden_titles):
                    continue
                if not _tokens_match_metadata(item, tokens):
                    continue
                seen_abs_ids.add(iid)
                results.append({
                    "title": title,
                    "author": item.get("author", ""),
                    "coverUrl": item.get("coverUrl", ""),
                    "source": "abs",
                    "itemId": iid,
                })

    if include_ebooks:
        # Prefer collection items (seriesName / genres already stamped).
        try:
            coll = await kavita_collection(user=user, db=db)
            ebook_items = coll.get("items") or []
        except Exception:
            logger.debug("Kavita collection for search failed; falling back", exc_info=True)
            ebook_items = []

        if ebook_items:
            seen_kavita_keys: set[str] = set()
            for item in ebook_items:
                name = item.get("title") or ""
                if not name or _is_hidden(name, hidden_titles):
                    continue
                sid = item.get("seriesId")
                # One hit per volume/file (multi-volume series expand on the shelf).
                file_key = item.get("fileKey") or item.get("fileName") or ""
                kavita_key = f"{sid}:{file_key or item.get('chapterId') or item.get('volumeId') or 's'}"
                if kavita_key in seen_kavita_keys:
                    continue
                if not _tokens_match_metadata(item, tokens):
                    continue
                seen_kavita_keys.add(kavita_key)
                if isinstance(sid, int):
                    seen_kavita_ids.add(sid)
                results.append({
                    "title": name,
                    "author": item.get("author", ""),
                    "coverUrl": item.get("coverUrl", ""),
                    "source": "kavita",
                    "seriesId": sid,
                    "chapterId": item.get("chapterId"),
                    "volumeId": item.get("volumeId"),
                    "fileKey": item.get("fileKey"),
                    "fileName": item.get("fileName"),
                })
        else:
            # Avoid per-series volume fetches during search (very slow). Collection
            # is the indexed path; if it's empty, return ABS/RD hits only.
            logger.debug("Kavita collection empty during search; skipping series fallback")

    # RD streaming library: include for "all" or "audiobooks"
    if media in ("all", "audiobooks"):
        stmt = (
            select(StreamingLibraryItem)
            .where(StreamingLibraryItem.user_id == user.id)
            .order_by(StreamingLibraryItem.updated_at.desc())
            .limit(200)
        )
        rows = (await db.execute(stmt)).scalars().all()
        matched = 0
        for item in rows:
            if matched >= 20:
                break
            probe = {
                "title": item.title or "",
                "author": item.author or "",
                "genre": item.genre or "",
                "seriesName": getattr(item, "series_name", None) or "",
            }
            if not _tokens_match_metadata(probe, tokens):
                # Fallback substring on title/author for multi-word phrases.
                ql = q.casefold()
                if ql not in (item.title or "").casefold() and ql not in (item.author or "").casefold():
                    continue
            matched += 1
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


@router.post("/refresh")
async def trigger_library_refresh(user: User = Depends(get_current_user)):
    """Kick the serialized ABS → Kavita refresh pipeline (coalesced, non-blocking)."""
    from app.services import library_refresh as refresh_pipeline

    kick = refresh_pipeline.kick()
    return {
        "ok": bool(kick.get("ok")),
        "message": kick.get("message") or "Library refresh",
        "started": bool(kick.get("started")),
        "already_running": bool(kick.get("already_running")),
        "cooldown": bool(kick.get("cooldown")),
        "deferred": bool(kick.get("started") or kick.get("already_running")),
    }


@router.get("/refresh/status")
async def library_refresh_status(user: User = Depends(get_current_user)):
    """Current phase of the refresh pipeline (idle | abs | kavita)."""
    from app.services import library_refresh as refresh_pipeline

    return refresh_pipeline.get_status()


@router.post("/abs/scan")
async def trigger_abs_scan(
    wait: bool = Query(
        True,
        description="Deprecated — scans now run via the serialized refresh pipeline.",
    ),
    user: User = Depends(get_current_user),
):
    """Legacy entry point (older mobile builds). Joins the serialized pipeline.

    Previously this ran an ABS scan while ``POST /library/kavita/scan`` ran a
    Kavita scan in parallel — two full scans at once froze the Pi. Both legacy
    endpoints now coalesce onto the single ABS → Kavita pipeline.
    """
    from app.services import library_refresh as refresh_pipeline

    kick = refresh_pipeline.kick()
    return {
        "ok": bool(kick.get("ok")),
        "message": kick.get("message") or "Library scan",
        "scan_ran": bool(kick.get("started")),
        "scan_complete": False,
        "timed_out": False,
        "waited_seconds": 0.0,
        "items_total": None,
        "deferred": bool(kick.get("started") or kick.get("already_running")),
        "already_running": bool(kick.get("already_running")),
    }


@router.post("/kavita/scan")
async def trigger_kavita_scan(user: User = Depends(get_current_user)):
    """Legacy entry point (older mobile builds). Joins the serialized pipeline."""
    from app.services import library_refresh as refresh_pipeline

    kick = refresh_pipeline.kick()
    return {"ok": bool(kick.get("ok")), "message": kick.get("message") or "Library refresh"}


# --------------- Reader proxy (Kavita Book API) ---------------

@router.get("/reader/cover/ebook")
async def proxy_kavita_ebook_cover(
    series_id: int = Query(..., alias="seriesId", description="Series ID (always required)"),
    volume_id: int | None = Query(None, alias="volumeId"),
    chapter_id: int | None = Query(None, alias="chapterId"),
):
    """Try volume, then chapter, then series cover. Returns first successful image."""
    import httpx

    kavita_url, kavita_key, _ = await kavita._conn()
    if not kavita_url:
        raise HTTPException(status_code=502, detail="Kavita not configured")
    urls_to_try: list[tuple[str, str]] = []
    if volume_id:
        urls_to_try.append((f"{kavita_url}/api/Image/volume-cover?volumeId={volume_id}", "volume"))
    if chapter_id:
        urls_to_try.append((f"{kavita_url}/api/Image/chapter-cover?chapterId={chapter_id}", "chapter"))
    urls_to_try.append((f"{kavita_url}/api/Image/series-cover?seriesId={series_id}", "series"))

    headers = {"x-api-key": kavita_key} if kavita_key else {}
    api_key_param = f"&apiKey={kavita_key}" if kavita_key else ""
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
    _dead = ("failed", "admin_rejected", "cancelled", "quarantined")
    others = (
        await db.execute(
            select(DownloadRequest.title).where(
                DownloadRequest.is_private == True,  # noqa: E712
                DownloadRequest.user_id != current_user_id,
                DownloadRequest.status.notin_(_dead),
            )
        )
    ).scalars().all()
    mine = (
        await db.execute(
            select(DownloadRequest.title).where(
                DownloadRequest.user_id == current_user_id,
                DownloadRequest.status.notin_(_dead),
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
        select(DownloadRequest.title).where(
            DownloadRequest.status.notin_(("failed", "admin_rejected", "cancelled", "quarantined"))
        )
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


@router.get("/owned-uploads/allowed")
async def owned_uploads_allowed(user: User = Depends(get_current_user)):
    """Whether the current user may POST /owned-uploads."""
    from app.services import library_ingest

    allowed = await library_ingest.user_may_upload_owned(user)
    return {"allowed": allowed, "allow_user_audiobook_upload": allowed}


@router.post("/owned-uploads")
async def owned_audiobook_upload(
    user: User = Depends(get_current_user),
    files: list[UploadFile] = File(...),
    title: str | None = Form(None),
    author: str | None = Form(None),
):
    """Upload owned audiobook files → staging → forge (no debrid).

    Admins always allowed; non-admins require ``allow_user_audiobook_upload``.
    """
    from app.services import library_ingest

    if not await library_ingest.user_may_upload_owned(user):
        raise HTTPException(
            status_code=403,
            detail="Owned audiobook upload is disabled for users",
        )
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")

    blobs: list[tuple[str, bytes]] = []
    for uf in files:
        name = (uf.filename or "audio").strip() or "audio"
        data = await uf.read()
        if not data:
            continue
        blobs.append((name, data))
    if not blobs:
        raise HTTPException(status_code=400, detail="Uploaded files were empty")

    book_title = (title or "").strip()
    if not book_title:
        # Derive from first audio-ish filename stem
        for name, _ in blobs:
            if library_ingest.is_audio_filename(name):
                book_title = Path(name).stem
                break
        if not book_title:
            book_title = Path(blobs[0][0]).stem or "Owned audiobook"

    book_author = (author or "").strip() or None

    # Stage + create request first without forge, then forge in background so
    # the HTTP response returns promptly for large uploads.
    result = await library_ingest.ingest_uploaded_audiobook(
        user_id=user.id,
        title=book_title,
        author=book_author,
        file_blobs=blobs,
        kick_forge=False,
    )
    request_id = int(result["id"])
    staging_path = result.get("staging_path")

    async def _forge() -> None:
        try:
            from app.services.forge_pipeline import (
                audiobook_staging_dir,
                resolve_staging_dir,
                run_forge_after_download,
            )

            staging = audiobook_staging_dir(request_id, book_title)
            if staging_path:
                try:
                    staging = resolve_staging_dir(str(staging_path))
                except FileNotFoundError:
                    pass
            await run_forge_after_download(
                request_id,
                staging=staging,
                user_id=user.id,
                title=book_title,
                author=book_author,
                resume_from="metadata",
            )
        except Exception:
            logger.exception("Owned upload forge failed for request %s", request_id)

    asyncio.create_task(_forge())

    return {
        "ok": True,
        "id": request_id,
        "title": book_title,
        "author": book_author,
        "status": result.get("status") or "metadata_forge",
        "file_count": result.get("file_count") or len(blobs),
        "staging_path": staging_path,
        "message": "Upload staged — forge started in background",
    }


# --- Ebook reading progress (Continue Reading sync) ---


class EbookProgressBody(BaseModel):
    chapter_id: int
    page: int = 0
    viewport_page: int = 0
    total_viewport_pages: int | None = None
    total_kavita_pages: int | None = None
    book_title: str = ""
    series_name: str | None = None
    cover_url: str = ""
    cfi: str | None = None  # EPUB CFI for exact resume
    hidden: bool = False
    last_read_at: float | None = None  # epoch ms from client


def _normalize_cfi(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Cap pathological payloads; CFIs are typically well under this.
    return text[:8192]


def _serialize_ebook_progress(row) -> dict:
    last = row.last_read_at
    if last is not None and getattr(last, "tzinfo", None) is None:
        last = last.replace(tzinfo=timezone.utc)
    cfi = getattr(row, "cfi", None)
    return {
        "chapterId": row.chapter_id,
        "page": int(row.page or 0),
        "viewportPage": int(row.viewport_page or 0),
        "totalViewportPages": row.total_viewport_pages,
        "totalKavitaPages": row.total_kavita_pages,
        "bookTitle": row.book_title or "",
        "seriesName": row.series_name,
        "coverUrl": row.cover_url or "",
        "cfi": cfi or None,
        "hidden": bool(row.hidden),
        "lastReadAt": int(last.timestamp() * 1000) if last else 0,
    }


@router.get("/reading-progress")
async def list_reading_progress(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
):
    """Continue Reading shelf — server-synced ebook progress for this account."""
    from app.models import EbookReadingProgress

    rows = (
        await db.execute(
            select(EbookReadingProgress)
            .where(
                EbookReadingProgress.user_id == user.id,
                EbookReadingProgress.hidden.is_(False),
            )
            .order_by(EbookReadingProgress.last_read_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {"items": [_serialize_ebook_progress(r) for r in rows]}


@router.put("/reading-progress/{chapter_id}")
async def upsert_reading_progress(
    chapter_id: int,
    body: EbookProgressBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upsert ebook progress. Keeps the newer last_read_at when racing devices."""
    from app.models import EbookReadingProgress

    cid = int(chapter_id)
    existing = (
        await db.execute(
            select(EbookReadingProgress).where(
                EbookReadingProgress.user_id == user.id,
                EbookReadingProgress.chapter_id == cid,
            )
        )
    ).scalar_one_or_none()

    client_ts = None
    if body.last_read_at is not None:
        try:
            client_ts = datetime.fromtimestamp(
                float(body.last_read_at) / 1000.0, tz=timezone.utc
            )
        except (TypeError, ValueError, OSError):
            client_ts = None
    now = datetime.now(timezone.utc)
    last_read = client_ts or now

    if existing:
        # Keep server row if it is newer than the client's stamp.
        # Normalize naive SQLite datetimes so aware/naive compare never 500s.
        existing_ts = existing.last_read_at
        if existing_ts is not None and existing_ts.tzinfo is None:
            existing_ts = existing_ts.replace(tzinfo=timezone.utc)
        if (
            existing_ts
            and client_ts
            and existing_ts > client_ts
            and (existing_ts - client_ts).total_seconds() > 2
        ):
            return {"item": _serialize_ebook_progress(existing), "kept": "server"}
        existing.page = int(body.page or 0)
        existing.viewport_page = int(body.viewport_page or 0)
        existing.total_viewport_pages = body.total_viewport_pages
        existing.total_kavita_pages = body.total_kavita_pages
        if body.book_title:
            existing.book_title = body.book_title[:512]
        if body.series_name is not None:
            existing.series_name = (body.series_name or None) and body.series_name[:512]
        if body.cover_url:
            existing.cover_url = body.cover_url[:1024]
        # Preserve prior CFI when client omits it (e.g. PDF progress writes).
        if body.cfi is not None:
            existing.cfi = _normalize_cfi(body.cfi)
        existing.hidden = bool(body.hidden)
        existing.last_read_at = last_read
        await db.commit()
        await db.refresh(existing)
        return {"item": _serialize_ebook_progress(existing), "kept": "client"}

    row = EbookReadingProgress(
        user_id=user.id,
        chapter_id=cid,
        page=int(body.page or 0),
        viewport_page=int(body.viewport_page or 0),
        total_viewport_pages=body.total_viewport_pages,
        total_kavita_pages=body.total_kavita_pages,
        book_title=(body.book_title or "")[:512],
        series_name=(body.series_name or None) and (body.series_name or "")[:512],
        cover_url=(body.cover_url or "")[:1024],
        cfi=_normalize_cfi(body.cfi),
        hidden=bool(body.hidden),
        last_read_at=last_read,
    )
    db.add(row)
    try:
        await db.commit()
    except Exception:
        # Concurrent insert race on (user_id, chapter_id) — retry as update.
        await db.rollback()
        existing = (
            await db.execute(
                select(EbookReadingProgress).where(
                    EbookReadingProgress.user_id == user.id,
                    EbookReadingProgress.chapter_id == cid,
                )
            )
        ).scalar_one_or_none()
        if not existing:
            raise
        existing.page = int(body.page or 0)
        existing.viewport_page = int(body.viewport_page or 0)
        existing.total_viewport_pages = body.total_viewport_pages
        existing.total_kavita_pages = body.total_kavita_pages
        if body.book_title:
            existing.book_title = body.book_title[:512]
        if body.series_name is not None:
            existing.series_name = (body.series_name or None) and body.series_name[:512]
        if body.cover_url:
            existing.cover_url = body.cover_url[:1024]
        if body.cfi is not None:
            existing.cfi = _normalize_cfi(body.cfi)
        existing.hidden = bool(body.hidden)
        existing.last_read_at = last_read
        await db.commit()
        await db.refresh(existing)
        return {"item": _serialize_ebook_progress(existing), "kept": "client"}
    await db.refresh(row)
    return {"item": _serialize_ebook_progress(row), "kept": "client"}


@router.delete("/reading-progress/{chapter_id}")
async def delete_reading_progress(
    chapter_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models import EbookReadingProgress

    row = (
        await db.execute(
            select(EbookReadingProgress).where(
                EbookReadingProgress.user_id == user.id,
                EbookReadingProgress.chapter_id == int(chapter_id),
            )
        )
    ).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"ok": True}


@router.post("/reading-progress/{chapter_id}/hide")
async def hide_reading_progress(
    chapter_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models import EbookReadingProgress

    row = (
        await db.execute(
            select(EbookReadingProgress).where(
                EbookReadingProgress.user_id == user.id,
                EbookReadingProgress.chapter_id == int(chapter_id),
            )
        )
    ).scalar_one_or_none()
    if row:
        row.hidden = True
        await db.commit()
    return {"ok": True}
