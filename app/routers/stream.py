"""Streaming endpoints for audiobook playback via ABS or Real-Debrid."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import Optional
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, Response
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, async_session
from app.models import User, StreamHistory, StreamingLibraryItem, ABSPlayTracking
from app.utils.auth import get_current_user, ALGORITHM
from app.services import audiobookshelf, debrid, debrid_tokens, real_debrid, prowlarr

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/stream", tags=["stream"])

AUDIO_EXTENSIONS = re.compile(
    r"\.(mp3|m4a|m4b|ogg|opus|flac|wav|wma|aac|mp4)$", re.IGNORECASE
)
ARCHIVE_EXTENSIONS = re.compile(
    r"\.(rar|zip|7z|tar|gz|bz2|r\d{2})$", re.IGNORECASE
)

# In-memory store for async stream resolution tasks
_stream_tasks: dict[str, dict] = {}

# Legacy: maps random proxy tokens to RD CDN URLs (kept for in-flight sessions;
# new code uses stable, DB-backed proxy URLs that survive restarts).
_rd_proxy_urls: dict[str, str] = {}

# Shared pooled client for upstream audio streaming (RD CDN + ABS). A fresh
# client per request meant a new TLS handshake for every playback range request
# and every 8 MB cache chunk — slow and flaky on a Pi. Keepalive reuses the
# connection across chunks.
_stream_client: httpx.AsyncClient | None = None


def _get_stream_client() -> httpx.AsyncClient:
    global _stream_client
    if _stream_client is None:
        _stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=90.0, write=90.0, pool=30.0),
            limits=httpx.Limits(
                max_connections=16,
                max_keepalive_connections=8,
                keepalive_expiry=60.0,
            ),
            follow_redirects=True,
        )
    return _stream_client


# --------------- Stable RD proxy URLs (survive restarts, self-healing) ---------------

def _proxy_sig(kind: str, row_id: int, index: int) -> str:
    msg = f"rdproxy:{kind}:{row_id}:{index}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()[:16]


def stable_proxy_url(kind: str, row_id: int, index: int) -> str:
    """Proxy URL tied to a DB row + track index instead of an in-memory token.
    kind: 'h' = StreamHistory, 'l' = StreamingLibraryItem."""
    return f"/api/stream/rd/proxy/{kind}/{row_id}/{index}/{_proxy_sig(kind, row_id, index)}"


def tracks_with_stable_urls(kind: str, row_id: int, tracks: list[dict]) -> list[dict]:
    """Return a copy of tracks with contentUrl replaced by stable proxy URLs."""
    out = []
    for i, t in enumerate(tracks):
        c = dict(t)
        c["contentUrl"] = stable_proxy_url(kind, row_id, i)
        out.append(c)
    return out


async def _user_from_token_str(token: str | None, db: AsyncSession) -> User | None:
    """Resolve a user from a raw JWT string (for sendBeacon requests that can't set headers)."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            return None
        user_id = int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    return user if user and user.is_active else None


class ABSPlaybackResponse(BaseModel):
    sessionId: str
    tracks: list[dict]
    startOffset: float
    coverUrl: str
    title: str
    author: str
    duration: float


class RDResolveRequest(BaseModel):
    magnet_link: Optional[str] = None
    download_url: Optional[str] = None
    title: str = "Unknown"
    author: str = ""
    cover_url: str = ""
    indexer: str = ""
    # "" = auto-pick (cached provider wins, then user preference); or "rd" / "torbox"
    provider: str = ""


class RDStreamResponse(BaseModel):
    streamUrl: str
    filename: str
    filesize: int


class SyncRequest(BaseModel):
    currentTime: float
    duration: float


class SmartStreamRequest(BaseModel):
    title: str
    author: str = ""
    cover_url: str = ""
    subtitle: str = ""
    series_name: str = ""
    series_index: str = ""


class RDProgressSyncRequest(BaseModel):
    stream_history_id: int
    progress_seconds: float
    total_seconds: float = 0
    current_track_index: int = 0
    track_position_seconds: float = 0
    # Optional: per-track durations discovered by the client, so future resumes
    # can locate the right track even before probing.
    track_durations: Optional[list[float]] = None


# --------------- Stream History & Smart Stream ---------------

@router.post("/rd/smart-stream")
async def smart_stream(
    body: SmartStreamRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Smart stream: searches Prowlarr for audiobook results, iterates through
    them trying each one on a debrid provider until one resolves. Returns a
    task_id for polling."""
    await debrid_tokens.apply_tokens_for_user_id(user.id)
    task_id = uuid.uuid4().hex[:12]
    _stream_tasks[task_id] = {
        "status": "searching",
        "detail": "Searching for available streams...",
        "progress": 0,
        "title": body.title,
        "tracks": None,
        "error": None,
        "stream_history_id": None,
    }
    asyncio.create_task(
        _smart_stream_background(
            task_id, body.title, body.author, body.cover_url, user.id,
            subtitle=body.subtitle, series_name=body.series_name, series_index=body.series_index,
            preferred_provider=getattr(user, "preferred_debrid", "rd") or "rd",
        )
    )
    return {"taskId": task_id, "status": "searching", "detail": "Searching for available streams..."}


@router.get("/rd/history")
async def get_stream_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all stream history for the current user (for continue-listening and badges)."""
    stmt = (
        select(StreamHistory)
        .where(StreamHistory.user_id == user.id)
        .order_by(StreamHistory.updated_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_serialize_history(h) for h in rows]}


@router.get("/rd/history/in-progress")
async def get_rd_in_progress(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get RD streams the user has started listening to (for continue-listening)."""
    stmt = (
        select(StreamHistory)
        .where(
            and_(
                StreamHistory.user_id == user.id,
                StreamHistory.progress_seconds > 0,
                StreamHistory.hidden.is_(False),
                StreamHistory.status.in_(["playing", "paused", "resolved"]),
            )
        )
        .order_by(StreamHistory.updated_at.desc())
        .limit(10)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_serialize_history(h) for h in rows]}


async def _apply_progress_sync(body: RDProgressSyncRequest, user: User, db: AsyncSession) -> None:
    result = await db.execute(
        select(StreamHistory).where(
            and_(StreamHistory.id == body.stream_history_id, StreamHistory.user_id == user.id)
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Stream history not found")
    # Safety net: a client whose resume was still buffering used to report a
    # near-zero position, silently wiping hours of progress. Intentional
    # restarts go through the clear-progress endpoint instead, so refuse
    # "back to the very start" syncs when real progress exists.
    looks_like_reset = (
        body.progress_seconds < 10
        and body.track_position_seconds < 10
        and body.current_track_index == 0
    )
    has_real_progress = item.progress_seconds > 120 or item.current_track_index > 0
    if looks_like_reset and has_real_progress:
        logger.info(
            "Ignoring suspicious progress reset for history %s (saved %.0fs/track %d)",
            item.id, item.progress_seconds, item.current_track_index,
        )
        return
    item.progress_seconds = body.progress_seconds
    if body.total_seconds > 0:
        item.total_seconds = body.total_seconds
    item.current_track_index = body.current_track_index
    item.track_position_seconds = body.track_position_seconds
    item.status = "playing"
    item.hidden = False  # playing again un-hides it from Continue Listening
    # Persist track durations discovered by the client so future resumes can
    # map global progress to the right track before probing.
    if body.track_durations and item.tracks_json:
        try:
            tracks = json.loads(item.tracks_json)
            if len(body.track_durations) == len(tracks):
                changed = False
                for t, d in zip(tracks, body.track_durations):
                    if d and d > 0 and not t.get("duration"):
                        t["duration"] = d
                        changed = True
                if changed:
                    item.tracks_json = json.dumps(tracks)
        except Exception:
            pass
    await db.commit()


@router.post("/rd/history/sync")
async def sync_rd_progress(
    body: RDProgressSyncRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save playback progress for an RD stream."""
    await _apply_progress_sync(body, user, db)
    return {"status": "ok"}


@router.post("/rd/history/sync-beacon")
async def sync_rd_progress_beacon(
    body: RDProgressSyncRequest,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Progress sync for navigator.sendBeacon (can't set Authorization headers)."""
    user = await _user_from_token_str(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    await _apply_progress_sync(body, user, db)
    return {"status": "ok"}


@router.post("/rd/history/{history_id}/clear-progress")
async def clear_rd_progress(
    history_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset listening progress for an RD stream (keeps the stream resumable from zero)."""
    result = await db.execute(
        select(StreamHistory).where(
            and_(StreamHistory.id == history_id, StreamHistory.user_id == user.id)
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Stream history not found")
    item.progress_seconds = 0.0
    item.current_track_index = 0
    item.track_position_seconds = 0.0
    item.status = "resolved"
    item.hidden = False
    await db.commit()
    return {"status": "ok"}


@router.post("/rd/history/{history_id}/hide")
async def hide_rd_history(
    history_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hide an RD stream from Continue Listening without touching progress."""
    result = await db.execute(
        select(StreamHistory).where(
            and_(StreamHistory.id == history_id, StreamHistory.user_id == user.id)
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Stream history not found")
    item.hidden = True
    await db.commit()
    return {"status": "ok"}


@router.get("/rd/history/check")
async def check_stream_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all magnet/download URLs the user has previously streamed.
    Used by the frontend to badge results in the download panel."""
    stmt = (
        select(StreamHistory)
        .where(StreamHistory.user_id == user.id)
    )
    rows = (await db.execute(stmt)).scalars().all()
    known: dict[str, dict] = {}
    for h in rows:
        if h.magnet_link:
            known[h.magnet_link] = {"id": h.id, "status": h.status, "hasProgress": h.progress_seconds > 0}
        if h.download_url:
            known[h.download_url] = {"id": h.id, "status": h.status, "hasProgress": h.progress_seconds > 0}
    return {"known": known}


@router.post("/abs/{item_id}/warmup")
async def warmup_abs_playback(
    item_id: str,
    _user: User = Depends(get_current_user),
):
    """Prime ABS / storage by reading the start of the first audio file (helps when disks are spun down)."""
    ok = await audiobookshelf.warmup_item_playback(item_id)
    return {"ok": ok}


@router.post("/abs/{item_id}/play")
async def start_abs_playback(
    item_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start a playback session on Audiobookshelf and return track URLs.
    All URLs are proxied through this backend to avoid mixed-content blocking."""
    session = await audiobookshelf.start_playback_session(item_id)
    if not session:
        raise HTTPException(status_code=502, detail="Failed to start ABS playback session")

    audio_tracks = session.get("audioTracks", [])
    tracks = []
    for t in audio_tracks:
        content_url = t.get("contentUrl", "")
        if content_url and not content_url.startswith("http"):
            content_url = f"{settings.abs_url}{content_url}"
        ino = t.get("ino") or ""
        item_id_for_url = item_id
        file_id = ""
        if content_url:
            parts = content_url.rstrip("/").split("/")
            if len(parts) >= 2:
                file_id = parts[-1]
        proxy_url = f"/api/stream/abs/proxy/audio/{item_id_for_url}/{file_id}" if file_id else content_url
        tracks.append({
            "index": t.get("index", 0),
            "startOffset": t.get("startOffset", 0),
            "duration": t.get("duration", 0),
            "title": t.get("title") or t.get("metadata", {}).get("title") or f"Track {t.get('index', 0) + 1}",
            "contentUrl": proxy_url,
            "mimeType": t.get("mimeType", "audio/mpeg"),
        })

    lib_item = session.get("libraryItem", {})
    media = lib_item.get("media", {})
    meta = media.get("metadata", {})
    total_duration = session.get("duration") or media.get("duration") or 0

    cover_url = ""
    if lib_item.get("id"):
        cover_url = f"/api/stream/abs/proxy/cover/{lib_item['id']}"

    play_title = meta.get("title") or lib_item.get("title", "Unknown")
    play_author = meta.get("authorName") or ""

    existing = await db.execute(
        select(ABSPlayTracking).where(
            ABSPlayTracking.user_id == user.id,
            ABSPlayTracking.abs_item_id == item_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        row.title = play_title
        row.author = play_author
        row.hidden = False  # playing again un-hides it from Continue Listening
    else:
        db.add(ABSPlayTracking(
            user_id=user.id,
            abs_item_id=item_id,
            title=play_title,
            author=play_author,
        ))
    await db.commit()

    return ABSPlaybackResponse(
        sessionId=session.get("id", ""),
        tracks=tracks,
        startOffset=session.get("currentTime", 0),
        coverUrl=cover_url,
        title=play_title,
        author=play_author,
        duration=total_duration,
    )


@router.get("/abs/{item_id}/chapters")
async def get_abs_item_chapters(
    item_id: str,
    _user: User = Depends(get_current_user),
):
    """Audiobookshelf chapter markers for an item (seconds from book start)."""
    chapters = await audiobookshelf.get_item_chapters(item_id)
    if chapters is None:
        raise HTTPException(status_code=404, detail="Library item not found")
    return {"chapters": chapters}


def _abs_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.abs_api_key}"}


@router.get("/abs/proxy/audio/{item_id}/{file_id}")
async def proxy_abs_audio(
    item_id: str,
    file_id: str,
    request: Request,
):
    """Reverse-proxy an ABS audio file so the browser fetches it over HTTPS.
    Uses path params to avoid query-string encoding issues."""
    url = f"{settings.abs_url}/api/items/{item_id}/file/{file_id}"
    logger.info("ABS audio proxy: %s", url)

    headers = _abs_headers()
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = _get_stream_client()
    try:
        resp = await client.send(
            client.build_request("GET", url, headers=headers),
            stream=True,
        )
    except Exception as e:
        logger.error("ABS audio proxy failed: %s", e)
        raise HTTPException(status_code=502, detail="Failed to fetch audio from ABS")

    if resp.status_code == 416:
        content_range = resp.headers.get("content-range")
        await resp.aclose()
        return Response(
            status_code=416,
            headers={"content-range": content_range} if content_range else {},
        )

    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        logger.error("ABS returned %s for %s: %s", resp.status_code, url, body[:200])
        raise HTTPException(status_code=resp.status_code, detail="ABS file not found")

    # Same guard as RD: don't commit to a 206/200 until the first body byte
    # arrives — an empty stream poisons client chunk downloads.
    body_iter = resp.aiter_bytes(chunk_size=65536)
    first_chunk = b""
    try:
        first_chunk = await body_iter.__anext__()
    except StopAsyncIteration:
        first_chunk = b""
    except Exception as e:
        await resp.aclose()
        logger.warning("ABS audio stream died before first byte: %s", e)
        raise HTTPException(status_code=502, detail="ABS audio stream failed")

    content_length = resp.headers.get("content-length")
    if not first_chunk and content_length not in (None, "0"):
        await resp.aclose()
        logger.warning("ABS sent empty body for %s-byte response", content_length)
        raise HTTPException(status_code=502, detail="ABS audio stream empty")

    resp_headers: dict[str, str] = {}
    for key in ("content-type", "content-length", "content-range", "accept-ranges"):
        val = resp.headers.get(key)
        if val:
            resp_headers[key] = val
    if "accept-ranges" not in resp_headers:
        resp_headers["accept-ranges"] = "bytes"
    resp_headers["x-accel-buffering"] = "no"

    status_code = resp.status_code

    async def stream_body():
        try:
            if first_chunk:
                yield first_chunk
            async for chunk in body_iter:
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=status_code,
        headers=resp_headers,
    )


# Placeholder SVG when ABS cover is unreachable (avoids 502 console errors)
_ABS_COVER_PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300" viewBox="0 0 200 300">'
    b'<rect fill="#374151" width="200" height="300"/>'
    b'<path fill="#6b7280" d="M70 100h60v100H70z"/>'
    b'<path fill="#9ca3af" d="M75 110h50v15H75zm0 25h35v5H75zm0 15h45v5H75z"/>'
    b'</svg>'
)


@router.get("/abs/proxy/cover/{item_id}")
async def proxy_abs_cover(
    item_id: str,
):
    """Reverse-proxy an ABS cover image. Returns placeholder if ABS is unreachable."""
    url = f"{settings.abs_url}/api/items/{item_id}/cover"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_abs_headers(), timeout=15)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = (await e.response.aread()).decode("utf-8", errors="replace")[:200]
        logger.warning(
            "ABS cover proxy: %s returned %s (url=%s) body=%s",
            settings.abs_url, e.response.status_code, url, body,
        )
        return Response(
            content=_ABS_COVER_PLACEHOLDER,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception as e:
        logger.warning("ABS cover proxy failed (url=%s): %s", url, e)
        return Response(
            content=_ABS_COVER_PLACEHOLDER,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    content_type = resp.headers.get("content-type", "image/jpeg")
    return StreamingResponse(
        iter([resp.content]),
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _stream_rd_url(real_url: str, request: Request) -> Response | None:
    """Open a streaming proxy to an RD CDN URL. Returns None when RD rejects the
    URL (expired link) or the body dies before the first byte, so callers can
    refresh and retry."""
    headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = _get_stream_client()
    try:
        resp = await client.send(
            client.build_request("GET", real_url, headers=headers),
            stream=True,
        )
    except Exception as e:
        logger.warning("RD audio proxy connect failed: %s", e)
        return None

    if resp.status_code == 416:
        # Range beyond end of file — the link is fine, the client just read past
        # the end. Pass it through so downloaders know they're done (a refresh
        # here would pointlessly re-resolve healthy links).
        content_range = resp.headers.get("content-range")
        await resp.aclose()
        return Response(
            status_code=416,
            headers={"content-range": content_range} if content_range else {},
        )

    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        logger.warning("RD CDN returned %s: %s", resp.status_code, body[:200])
        return None

    # Pull the first body chunk BEFORE committing to a response. If the CDN
    # accepts the request but resets immediately (stale link), returning a 206
    # with an empty body poisons client downloads ("empty chunk received");
    # returning None here routes callers into the link-refresh path instead.
    body_iter = resp.aiter_bytes(chunk_size=65536)
    first_chunk = b""
    try:
        first_chunk = await body_iter.__anext__()
    except StopAsyncIteration:
        first_chunk = b""
    except Exception as e:
        await resp.aclose()
        logger.warning("RD CDN stream died before first byte: %s", e)
        return None

    content_length = resp.headers.get("content-length")
    if not first_chunk and content_length not in (None, "0"):
        await resp.aclose()
        logger.warning("RD CDN sent empty body for %s-byte response", content_length)
        return None

    resp_headers: dict[str, str] = {}
    for key in ("content-type", "content-length", "content-range", "accept-ranges"):
        val = resp.headers.get(key)
        if val:
            resp_headers[key] = val
    if "accept-ranges" not in resp_headers:
        resp_headers["accept-ranges"] = "bytes"
    # Tell nginx not to buffer: with default buffering it slurps the whole
    # upstream stream into SD-card temp files as fast as RD serves it, starving
    # the Pi's bandwidth and disk while the client plays at 1x.
    resp_headers["x-accel-buffering"] = "no"

    status_code = resp.status_code

    async def stream_body():
        try:
            if first_chunk:
                yield first_chunk
            async for chunk in body_iter:
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=status_code,
        headers=resp_headers,
    )


# Per-row refresh locks so parallel track requests don't stampede Real-Debrid
_refresh_locks: dict[str, asyncio.Lock] = {}
_last_refresh_at: dict[str, float] = {}


async def _fresh_audio_urls(
    rd_torrent_id: str | None,
    magnet_link: str | None,
    provider: str = "rd",
) -> list[str] | None:
    """Fresh, playable CDN URLs for a torrent's audio files (provider-aware).

    Fast path: the torrent still exists in the account -> re-unrestrict (~2s).
    Slow path: re-add the magnet (instant for previously-downloaded/cached torrents)."""
    return await debrid.fresh_audio_urls(provider, rd_torrent_id, magnet_link, AUDIO_EXTENSIONS)


def _merge_fresh_urls(tracks: list[dict], fresh_urls: list[str]) -> bool:
    """Update track contentUrls in place from fresh RD URLs. Matches by filename
    first, then by position. Returns True when anything changed."""
    by_name = {}
    for u in fresh_urls:
        by_name[unquote(u.rsplit("/", 1)[-1].split("?")[0]).lower()] = u
    changed = False
    used: set[str] = set()
    for i, t in enumerate(tracks):
        old = t.get("contentUrl", "")
        old_name = unquote(old.rsplit("/", 1)[-1].split("?")[0]).lower() if old.startswith("http") else ""
        new_url = by_name.get(old_name)
        if not new_url and i < len(fresh_urls):
            new_url = fresh_urls[i]
        if new_url and new_url not in used and new_url != old:
            t["contentUrl"] = new_url
            used.add(new_url)
            changed = True
    return changed


async def _refresh_row_tracks(kind: str, row_id: int) -> list[dict] | None:
    """Refresh a StreamHistory / StreamingLibraryItem row's RD links and persist them.
    Returns the refreshed raw tracks, or None on failure."""
    key = f"{kind}:{row_id}"
    lock = _refresh_locks.setdefault(key, asyncio.Lock())
    async with lock:
        async with async_session() as db:
            row = await _load_proxy_row(kind, row_id, db)
            if not row or not row.tracks_json:
                return None
            try:
                tracks = json.loads(row.tracks_json)
            except Exception:
                return None

            # Another request may have just refreshed while we waited on the lock
            if time.monotonic() - _last_refresh_at.get(key, 0) < 20:
                return tracks

            # Proxy requests are signature-authenticated (no JWT) — use the
            # row owner's library-group API keys for the refresh.
            await debrid_tokens.apply_tokens_for_user_id(row.user_id)
            fresh = await _fresh_audio_urls(
                row.rd_torrent_id, row.magnet_link,
                provider=getattr(row, "debrid_provider", "rd") or "rd",
            )
            if not fresh:
                return None
            _merge_fresh_urls(tracks, fresh)
            row.tracks_json = json.dumps(tracks)
            await db.commit()
            _last_refresh_at[key] = time.monotonic()
            return tracks


async def _load_proxy_row(kind: str, row_id: int, db: AsyncSession):
    model = StreamHistory if kind == "h" else StreamingLibraryItem
    result = await db.execute(select(model).where(model.id == row_id))
    return result.scalar_one_or_none()


@router.get("/rd/proxy/{kind}/{row_id}/{index}/{sig}")
async def proxy_rd_audio_stable(
    kind: str,
    row_id: int,
    index: int,
    sig: str,
    request: Request,
):
    """Reverse-proxy an RD CDN audio file via a stable, DB-backed URL.

    Survives server restarts and self-heals expired RD links: on failure it
    re-unrestricts from the saved torrent id (fast) or re-adds the magnet."""
    if kind not in ("h", "l") or not hmac.compare_digest(sig, _proxy_sig(kind, row_id, index)):
        raise HTTPException(status_code=404, detail="Invalid stream URL")

    async with async_session() as db:
        row = await _load_proxy_row(kind, row_id, db)
    if not row or not row.tracks_json:
        raise HTTPException(status_code=404, detail="Stream not found")
    try:
        tracks = json.loads(row.tracks_json)
    except Exception:
        raise HTTPException(status_code=404, detail="Stream tracks unavailable")
    if index < 0 or index >= len(tracks):
        raise HTTPException(status_code=404, detail="Track not found")

    real_url = tracks[index].get("contentUrl", "")
    if real_url.startswith("http"):
        resp = await _stream_rd_url(real_url, request)
        if resp is not None:
            return resp

    # Link dead (or legacy row that stored proxy paths) -> refresh and retry once
    refreshed = await _refresh_row_tracks(kind, row_id)
    if refreshed and index < len(refreshed):
        real_url = refreshed[index].get("contentUrl", "")
        if real_url.startswith("http"):
            resp = await _stream_rd_url(real_url, request)
            if resp is not None:
                return resp

    raise HTTPException(status_code=502, detail="Stream unavailable — debrid links could not be refreshed")


@router.get("/rd/proxy/{token}")
async def proxy_rd_audio(
    token: str,
    request: Request,
):
    """Legacy reverse-proxy using in-memory tokens (active sessions from before
    the stable-URL scheme). New streams use /rd/proxy/{kind}/{row_id}/{index}/{sig}."""
    real_url = _rd_proxy_urls.get(token)
    if not real_url:
        raise HTTPException(status_code=404, detail="Stream token not found or expired")

    resp = await _stream_rd_url(real_url, request)
    if resp is None:
        raise HTTPException(status_code=502, detail="Failed to fetch audio from Real-Debrid")
    return resp


@router.post("/abs/{session_id}/sync")
async def sync_abs_session(
    session_id: str,
    body: SyncRequest,
    _user: User = Depends(get_current_user),
):
    ok = await audiobookshelf.sync_session(session_id, body.currentTime, body.duration)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to sync session")
    return {"status": "ok"}


@router.post("/abs/{session_id}/close")
async def close_abs_session(
    session_id: str,
    body: SyncRequest,
    _user: User = Depends(get_current_user),
):
    ok = await audiobookshelf.close_session(session_id, body.currentTime, body.duration)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to close session")
    return {"status": "ok"}


@router.post("/abs/{session_id}/close-beacon")
async def close_abs_session_beacon(
    session_id: str,
    body: SyncRequest,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Session close for navigator.sendBeacon (can't set Authorization headers)."""
    user = await _user_from_token_str(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    await audiobookshelf.close_session(session_id, body.currentTime, body.duration)
    return {"status": "ok"}


@router.post("/abs/{session_id}/sync-beacon")
async def sync_abs_session_beacon(
    session_id: str,
    body: SyncRequest,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Progress sync for navigator.sendBeacon — saves position WITHOUT closing the
    session (used when the app is backgrounded but may keep playing)."""
    user = await _user_from_token_str(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    await audiobookshelf.sync_session(session_id, body.currentTime, body.duration)
    return {"status": "ok"}


@router.post("/abs/{item_id}/hide")
async def hide_abs_item(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hide an ABS item from this user's Continue Listening (progress preserved)."""
    result = await db.execute(
        select(ABSPlayTracking).where(
            ABSPlayTracking.user_id == user.id,
            ABSPlayTracking.abs_item_id == item_id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Item not tracked")
    row.hidden = True
    await db.commit()
    return {"status": "ok"}


@router.post("/abs/{item_id}/clear-progress")
async def clear_abs_progress(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear listening progress for an ABS item and remove it from this user's shelf."""
    result = await db.execute(
        select(ABSPlayTracking).where(
            ABSPlayTracking.user_id == user.id,
            ABSPlayTracking.abs_item_id == item_id,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    # Best-effort wipe on the ABS server (shared account); local tracking is already gone.
    abs_ok = await audiobookshelf.reset_item_progress(item_id)
    return {"status": "ok", "absReset": abs_ok}


@router.get("/abs/in-progress")
async def get_in_progress(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get items the user is currently listening to on ABS (filtered to user's plays)."""
    tracked = await db.execute(
        select(ABSPlayTracking.abs_item_id).where(
            ABSPlayTracking.user_id == user.id,
            ABSPlayTracking.hidden.is_(False),
        )
    )
    tracked_ids = {row[0] for row in tracked.all()}

    items = await audiobookshelf.get_items_in_progress()
    results = []
    for item in items:
        item_id = item.get("id", "")
        if item_id not in tracked_ids:
            continue
        media = item.get("media", {})
        meta = media.get("metadata", {})
        progress = item.get("userMediaProgress", {})
        results.append({
            "itemId": item_id,
            "title": meta.get("title") or "",
            "author": meta.get("authorName") or "",
            "coverUrl": f"/api/stream/abs/proxy/cover/{item_id}" if item_id else "",
            "progress": progress.get("progress", 0),
            "currentTime": progress.get("currentTime", 0),
            "duration": progress.get("duration") or media.get("duration") or 0,
            "isFinished": progress.get("isFinished", False),
        })
    return {"items": results}


@router.get("/abs/search")
async def search_abs_for_streaming(
    q: str = Query(..., min_length=2),
    _user: User = Depends(get_current_user),
):
    """Search ABS library and return streamable items."""
    items = await audiobookshelf.search_library_with_ids(q)
    return {"items": items}


@router.post("/rd/resolve")
async def resolve_rd_stream(
    body: RDResolveRequest,
    user: User = Depends(get_current_user),
):
    """Start resolving a magnet/torrent via Real-Debrid. Returns a task_id
    the frontend polls via /rd/status/{task_id} for progress updates."""
    if not body.magnet_link and not body.download_url:
        raise HTTPException(status_code=400, detail="Provide magnet_link or download_url")

    # Load the user's library-group API keys into context; the background task
    # inherits them via contextvars.
    await debrid_tokens.apply_tokens_for_user_id(user.id)

    task_id = uuid.uuid4().hex[:12]
    _stream_tasks[task_id] = {
        "status": "starting",
        "detail": "Sending to Real-Debrid...",
        "progress": 0,
        "title": body.title,
        "tracks": None,
        "error": None,
        "stream_history_id": None,
    }

    asyncio.create_task(
        _resolve_rd_background(
            task_id, body.magnet_link, body.download_url, body.title,
            user_id=user.id, indexer=body.indexer, author=body.author, cover_url=body.cover_url,
            provider=body.provider,
            preferred_provider=getattr(user, "preferred_debrid", "rd") or "rd",
        )
    )

    return {"taskId": task_id, "status": "starting", "detail": "Sending to debrid service..."}


@router.get("/rd/status/{task_id}")
async def rd_stream_status(
    task_id: str,
    _user: User = Depends(get_current_user),
):
    """Poll the status of an RD stream resolution task."""
    task = _stream_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    resp = {
        "taskId": task_id,
        "status": task["status"],
        "detail": task["detail"],
        "progress": task["progress"],
        "title": task["title"],
        "streamHistoryId": task.get("stream_history_id"),
        "provider": task.get("provider"),
    }
    if task["status"] == "ready":
        resp["tracks"] = task["tracks"]
        # Saved listening position so the client resumes instead of restarting
        resp["progressSeconds"] = task.get("progress_seconds", 0)
        resp["currentTrackIndex"] = task.get("current_track_index", 0)
        resp["trackPositionSeconds"] = task.get("track_position_seconds", 0)
        _stream_tasks.pop(task_id, None)
    elif task["status"] == "error":
        resp["error"] = task["error"]
        _stream_tasks.pop(task_id, None)
    return resp


async def _resolve_rd_background(
    task_id: str,
    magnet_link: str | None,
    download_url: str | None,
    title: str,
    user_id: int | None = None,
    indexer: str | None = None,
    author: str = "",
    cover_url: str = "",
    provider: str = "",
    preferred_provider: str = "rd",
):
    """Background coroutine that resolves the stream on a debrid provider
    (Real-Debrid or Torbox) and updates _stream_tasks."""
    task = _stream_tasks[task_id]
    try:
        magnet = magnet_link
        torrent_bytes: bytes | None = None

        if (
            not magnet
            and download_url
            and "audiobookbay" in (download_url or "").lower()
        ):
            task["detail"] = "Resolving AudioBook Bay magnet…"
            try:
                from app.services import audiobookbay

                m, _h = await audiobookbay.resolve_magnet_from_details(
                    download_url, title=title or ""
                )
                if m:
                    magnet = m
            except Exception as e:
                logger.debug("ABB magnet resolve failed for %s: %s", download_url, e)

        if not magnet and download_url:
            task["detail"] = "Resolving download link from indexer..."
            try:
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    resp = await client.get(download_url, timeout=60)
                    while resp.is_redirect:
                        location = resp.headers.get("location", "")
                        if location.startswith("magnet:"):
                            magnet = location
                            break
                        resp = await client.get(location, timeout=60)
                    else:
                        if resp.status_code >= 400:
                            task["status"] = "error"
                            task["error"] = f"Indexer returned error {resp.status_code}. The link may have expired."
                            return
                        raw = resp.content
                        ct = resp.headers.get("content-type", "")
                        if raw[:7] == b"magnet:" or "magnet" in ct:
                            magnet = raw.decode("utf-8", errors="ignore").strip()
                        elif b"announce" in raw or ct == "application/x-bittorrent":
                            torrent_bytes = raw
                        else:
                            task["status"] = "error"
                            task["error"] = "Could not extract magnet or torrent from indexer. Try a different result."
                            return
            except Exception as e:
                logger.error("Failed to resolve download URL: %s", e)
                task["status"] = "error"
                task["error"] = "Failed to reach the indexer. It may be down or blocked."
                return

        # Pick the provider: explicit choice > cached-on-provider > user preference
        chosen = debrid.normalize_provider(provider) if provider else None
        if not chosen:
            task["detail"] = "Checking debrid caches..."
            chosen = await debrid.pick_provider_for_magnet(magnet, preferred_provider)
        client = debrid.get_client(chosen)
        provider_label = debrid.PROVIDER_LABELS.get(chosen, "Real-Debrid")
        task["provider"] = chosen

        task["detail"] = f"Sending to {provider_label}..."
        task["status"] = "sending"

        if magnet:
            result = await client.add_magnet(magnet)
        elif torrent_bytes:
            if chosen == debrid.TORBOX:
                from app.services import torbox
                result = await torbox.add_torrent_bytes(torrent_bytes)
            else:
                result = await _upload_torrent_bytes_to_rd(torrent_bytes)
        else:
            task["status"] = "error"
            task["error"] = "No magnet link or torrent file available for this result."
            return

        torrent_id = result.get("id")
        if not torrent_id:
            task["status"] = "error"
            task["error"] = f"{provider_label} did not return a torrent ID"
            return

        # Inspect torrent files and prefer selecting only audio files
        task["detail"] = "Analyzing torrent contents..."
        info_pre = await client.get_torrent_info(torrent_id)
        rd_files = info_pre.get("files", [])

        audio_file_ids = []
        archive_file_names = []
        for f in rd_files:
            path = f.get("path", "")
            fname = path.rsplit("/", 1)[-1] if "/" in path else path
            fid = f.get("id")
            if AUDIO_EXTENSIONS.search(fname):
                audio_file_ids.append(str(fid))
            elif ARCHIVE_EXTENSIONS.search(fname):
                archive_file_names.append(fname)

        if audio_file_ids:
            await client.select_files(torrent_id, ",".join(audio_file_ids))
            logger.info("Selected %d audio files from torrent", len(audio_file_ids))
        elif archive_file_names:
            task["status"] = "error"
            task["error"] = (
                "This torrent only contains compressed files "
                f"({', '.join(archive_file_names[:3])}). "
                "Compressed archives can't be streamed directly — use the "
                "Request button to download and extract it instead."
            )
            return
        else:
            await client.select_files(torrent_id, "all")

        task["status"] = "downloading"
        task["detail"] = f"{provider_label} is downloading..."

        elapsed = 0.0
        timeout = 600.0
        interval = 3.0
        while elapsed < timeout:
            info = await client.get_torrent_info(torrent_id)
            rd_status = info.get("status")
            rd_progress = info.get("progress", 0)

            if rd_status == "downloaded":
                break
            if rd_status in ("magnet_error", "error", "virus", "dead"):
                task["status"] = "error"
                task["error"] = f"{provider_label} torrent failed: {rd_status}"
                return

            task["progress"] = rd_progress
            if rd_status == "downloading":
                speed = info.get("speed")
                speed_str = ""
                if speed and speed > 0:
                    speed_str = f" ({speed / 1024 / 1024:.1f} MB/s)"
                task["detail"] = f"{provider_label} downloading... {rd_progress}%{speed_str}"
            elif rd_status == "queued":
                task["detail"] = f"Queued on {provider_label}..."
            elif rd_status == "magnet_conversion":
                task["detail"] = "Converting magnet link..."
            elif rd_status == "waiting_files_selection":
                if audio_file_ids:
                    await client.select_files(torrent_id, ",".join(audio_file_ids))
                else:
                    await client.select_files(torrent_id, "all")
                task["detail"] = "Selecting files..."
            else:
                task["detail"] = f"{provider_label} status: {rd_status}"

            await asyncio.sleep(interval)
            elapsed += interval
        else:
            task["status"] = "error"
            task["error"] = f"Timed out waiting for {provider_label} to finish downloading"
            return

        task["status"] = "finalizing"
        task["detail"] = "Preparing stream links..."
        task["progress"] = 100

        links = info.get("links", [])
        if not links:
            task["status"] = "error"
            task["error"] = f"No files available from {provider_label}"
            return

        unrestricted = await asyncio.gather(
            *[client.unrestrict_link(link) for link in links],
            return_exceptions=True,
        )

        tracks = []
        track_idx = 0
        all_filenames = []
        has_archives = False
        for i, link_url in enumerate(unrestricted):
            if isinstance(link_url, Exception):
                logger.warning("Failed to unrestrict link %d: %s", i, link_url)
                continue
            filename = debrid.link_filename(links[i], link_url)
            all_filenames.append(filename)
            if ARCHIVE_EXTENSIONS.search(filename):
                has_archives = True
                continue
            if not AUDIO_EXTENSIONS.search(filename):
                continue
            tracks.append({
                "index": track_idx,
                "startOffset": 0,
                "duration": 0,
                "title": _clean_filename(filename),
                "contentUrl": link_url,
                "mimeType": _guess_mime(filename),
            })
            track_idx += 1

        if not tracks:
            if has_archives:
                task["status"] = "error"
                task["error"] = (
                    "This torrent contains compressed files "
                    f"({', '.join(f for f in all_filenames if ARCHIVE_EXTENSIONS.search(f))[:3]}). "
                    "Compressed archives can't be streamed directly — use the "
                    "Request button to download and extract it instead."
                )
            else:
                task["status"] = "error"
                task["error"] = "No audio files found. Files: " + ", ".join(all_filenames[:10])
            return

        serialized_tracks = tracks
        if user_id:
            try:
                # Save raw CDN URLs; clients receive stable proxy URLs that
                # survive restarts and self-heal when RD links expire.
                await _save_stream_history(
                    task, user_id, title, author, cover_url,
                    magnet_link, download_url, indexer, torrent_id, tracks,
                    provider=chosen,
                )
                if task.get("stream_history_id"):
                    serialized_tracks = tracks_with_stable_urls("h", task["stream_history_id"], tracks)
            except Exception as e:
                logger.warning("Failed to save stream history: %s", e)
        if serialized_tracks is tracks:
            _proxify_tracks(serialized_tracks)

        task["status"] = "ready"
        task["detail"] = "Ready to play!"
        task["tracks"] = serialized_tracks

    except Exception as e:
        logger.error("RD background resolve failed: %s", e, exc_info=True)
        task["status"] = "error"
        task["error"] = str(e)


async def _upload_torrent_bytes_to_rd(data: bytes) -> dict:
    """Upload raw .torrent bytes directly to Real-Debrid."""
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{real_debrid.BASE_URL}/torrents/addTorrent",
            headers=real_debrid._headers(),
            content=data,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


def _proxify_tracks(tracks: list[dict]) -> list[dict]:
    """Replace direct RD CDN URLs with backend proxy URLs."""
    for track in tracks:
        real_url = track.get("contentUrl", "")
        if real_url and not real_url.startswith("/api/"):
            token = uuid.uuid4().hex[:16]
            _rd_proxy_urls[token] = real_url
            track["contentUrl"] = f"/api/stream/rd/proxy/{token}"
    return tracks


def _clean_filename(filename: str) -> str:
    name = AUDIO_EXTENSIONS.sub("", filename)
    name = name.replace("_", " ").replace("-", " ").replace(".", " ")
    return name.strip() or filename


def _guess_mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "m4b": "audio/mp4",
        "ogg": "audio/ogg",
        "opus": "audio/opus",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "wma": "audio/x-ms-wma",
        "aac": "audio/aac",
        "mp4": "audio/mp4",
    }.get(ext, "audio/mpeg")


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


async def _save_stream_history(
    task: dict,
    user_id: int,
    title: str,
    author: str,
    cover_url: str,
    magnet_link: str | None,
    download_url: str | None,
    indexer: str | None,
    torrent_id: str | None,
    tracks: list[dict],
    provider: str = "rd",
):
    """Upsert a resolved stream into history so it can be resumed later.

    Re-resolving the same book updates the existing row (fresh links/torrent id)
    instead of creating a duplicate that would reset progress to zero. The task
    dict receives the history id plus saved progress so the client can resume."""
    from app.services.real_debrid import extract_info_hash

    async with async_session() as db:
        rows = (
            await db.execute(
                select(StreamHistory).where(StreamHistory.user_id == user_id)
            )
        ).scalars().all()

        new_hash = extract_info_hash(magnet_link, download_url=download_url)
        hist: StreamHistory | None = None
        for h in rows:
            if torrent_id and h.rd_torrent_id and h.rd_torrent_id == torrent_id:
                hist = h
                break
            if new_hash and extract_info_hash(h.magnet_link, download_url=h.download_url) == new_hash:
                hist = h
                break
        if hist is None:
            # Same book resolved from a different torrent: keep the progress
            for h in rows:
                if _norm_key(h.title) == _norm_key(title) and _norm_key(h.author) == _norm_key(author):
                    hist = h
                    break

        if hist is not None:
            # Preserve known track durations from the previous resolve when the
            # file list looks the same (same count) so resume stays accurate.
            try:
                old_tracks = json.loads(hist.tracks_json) if hist.tracks_json else []
            except Exception:
                old_tracks = []
            if len(old_tracks) == len(tracks):
                for old, new in zip(old_tracks, tracks):
                    if old.get("duration") and not new.get("duration"):
                        new["duration"] = old["duration"]
            hist.title = title
            hist.author = author or hist.author
            hist.cover_url = cover_url or hist.cover_url
            hist.magnet_link = magnet_link or hist.magnet_link
            hist.download_url = download_url or hist.download_url
            hist.indexer = indexer or hist.indexer
            hist.rd_torrent_id = torrent_id or hist.rd_torrent_id
            hist.debrid_provider = provider or hist.debrid_provider or "rd"
            hist.tracks_json = json.dumps(tracks)
            if hist.status not in ("playing", "paused"):
                hist.status = "resolved"
        else:
            hist = StreamHistory(
                user_id=user_id,
                title=title,
                author=author,
                cover_url=cover_url,
                magnet_link=magnet_link,
                download_url=download_url,
                indexer=indexer,
                rd_torrent_id=torrent_id,
                debrid_provider=provider or "rd",
                tracks_json=json.dumps(tracks),
                status="resolved",
            )
            db.add(hist)
        await db.commit()
        await db.refresh(hist)
        task["stream_history_id"] = hist.id
        task["progress_seconds"] = hist.progress_seconds
        task["current_track_index"] = hist.current_track_index
        task["track_position_seconds"] = hist.track_position_seconds


async def _smart_stream_background(
    task_id: str,
    title: str,
    author: str,
    cover_url: str,
    user_id: int,
    subtitle: str = "",
    series_name: str = "",
    series_index: str = "",
    preferred_provider: str = "rd",
):
    """Search Prowlarr for audiobook results, rank them with the same matching
    pipeline as the download panel, and try the best candidates on a debrid
    provider (cached provider wins, then user preference) in order."""
    from app.services.download_discovery import (
        build_audiobookbay_queries,
        filter_irrelevant_results,
        merge_indexer_results,
        rank_indexer_results,
        resolve_book_search_context,
    )

    task = _stream_tasks[task_id]
    try:
        task["detail"] = "Searching indexers..."

        ctx = resolve_book_search_context(
            title=title,
            subtitle=subtitle,
            author=author,
            series_name=series_name or None,
            series_index=series_index or None,
        )

        trusted_results: list = []
        general_results: list = []
        queries = build_audiobookbay_queries(ctx)
        try:
            trusted_results = await prowlarr.search_trusted_indexers_multi(queries)
        except Exception as e:
            logger.warning("Smart stream trusted search failed: %s", e)
        try:
            general_results = await prowlarr.search(f"{title} {author}".strip())
        except Exception as e:
            logger.warning("Smart stream general search failed: %s", e)

        results = merge_indexer_results(trusted_results, general_results)

        audiobooks = [
            r for r in results
            if r.get("mediaType") == "audiobook"
            and (r.get("magnetUrl") or r.get("downloadUrl"))
        ]

        if not audiobooks:
            task["status"] = "error"
            task["error"] = (
                "No audiobook streams found. Use the Find Downloads section "
                "to search manually and request a download instead."
            )
            return

        # Rank with the shared matching pipeline (book number, title, author) so
        # we try the RIGHT book first — seeders alone often picked a different
        # volume in the series.
        relevant, _ = filter_irrelevant_results(audiobooks, ctx)
        ranked = rank_indexer_results(relevant or audiobooks, ctx)
        strong = [r for r in ranked if r.get("matchTier") in ("exact", "likely")]
        candidates = strong or ranked
        if not candidates:
            candidates = sorted(audiobooks, key=lambda r: r.get("seeders", 0), reverse=True)

        # One batched cache check across all candidates so each try can
        # auto-pick the provider that already has the torrent.
        cached_by_provider: dict[str, set[str]] = {}
        if len(debrid.available_providers()) > 1:
            hashes = []
            for r in candidates[:8]:
                h = debrid.extract_info_hash(r.get("magnetUrl"), r.get("infoHash"), r.get("downloadUrl"))
                if h:
                    hashes.append(h)
            if hashes:
                cached_by_provider = await debrid.check_cached_all(hashes)

        total_tries = min(len(candidates), 8)
        for i, result in enumerate(candidates[:8]):
            magnet = result.get("magnetUrl")
            dl = result.get("downloadUrl")
            indexer = result.get("indexer", "")
            info_hash = debrid.extract_info_hash(magnet, result.get("infoHash"), dl)
            provider = debrid.pick_provider(info_hash, cached_by_provider, preferred_provider)
            task["detail"] = f"Trying stream {i + 1}/{total_tries} ({indexer})..."
            task["progress"] = int((i / total_tries) * 50)

            try:
                resolved = await _try_resolve_single(
                    magnet, dl, provider=provider, title=result.get("title") or title
                )
                if resolved:
                    tracks, resolved_torrent_id = resolved
                    task["provider"] = provider
                    serialized_tracks = tracks
                    try:
                        await _save_stream_history(
                            task, user_id, title, author, cover_url,
                            magnet, dl, indexer, resolved_torrent_id, tracks,
                            provider=provider,
                        )
                        if task.get("stream_history_id"):
                            serialized_tracks = tracks_with_stable_urls("h", task["stream_history_id"], tracks)
                    except Exception as e:
                        logger.warning("Failed to save smart stream history: %s", e)
                    if serialized_tracks is tracks:
                        _proxify_tracks(serialized_tracks)

                    task["status"] = "ready"
                    task["detail"] = "Ready to play!"
                    task["progress"] = 100
                    task["tracks"] = serialized_tracks
                    return
            except _ArchiveOnlyError:
                continue
            except Exception as e:
                logger.info("Smart stream: result %d (%s) failed: %s", i, indexer, e)
                continue

        task["status"] = "error"
        task["error"] = (
            "Could not find a streamable audiobook. The available torrents may "
            "contain only compressed archives. Use the Request button to download instead."
        )

    except Exception as e:
        logger.error("Smart stream failed: %s", e, exc_info=True)
        task["status"] = "error"
        task["error"] = str(e)


class _ArchiveOnlyError(Exception):
    pass


async def _try_resolve_single(
    magnet_link: str | None,
    download_url: str | None,
    provider: str = "rd",
    title: str = "",
) -> tuple[list[dict], str] | None:
    """Try to resolve a single result on a debrid provider.
    Returns (tracks, torrent_id) or None."""
    client_mod = debrid.get_client(provider)
    magnet = magnet_link
    torrent_bytes: bytes | None = None

    # Direct ABB detail pages need a hash scrape (Jackett normally does this).
    if (
        not magnet
        and download_url
        and "audiobookbay" in download_url.lower()
    ):
        try:
            from app.services import audiobookbay

            m, _h = await audiobookbay.resolve_magnet_from_details(download_url, title=title)
            if m:
                magnet = m
        except Exception as e:
            logger.debug("ABB magnet resolve failed for %s: %s", download_url, e)

    if not magnet and download_url:
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.get(download_url, timeout=30)
                while resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if location.startswith("magnet:"):
                        magnet = location
                        break
                    resp = await client.get(location, timeout=30)
                else:
                    if resp.status_code >= 400:
                        return None
                    raw = resp.content
                    ct = resp.headers.get("content-type", "")
                    if raw[:7] == b"magnet:" or "magnet" in ct:
                        magnet = raw.decode("utf-8", errors="ignore").strip()
                    elif b"announce" in raw or ct == "application/x-bittorrent":
                        torrent_bytes = raw
                    else:
                        return None
        except Exception:
            return None

    if magnet:
        result = await client_mod.add_magnet(magnet)
    elif torrent_bytes:
        if provider == debrid.TORBOX:
            from app.services import torbox
            result = await torbox.add_torrent_bytes(torrent_bytes)
        else:
            result = await _upload_torrent_bytes_to_rd(torrent_bytes)
    else:
        return None

    torrent_id = result.get("id")
    if not torrent_id:
        return None

    info_pre = await client_mod.get_torrent_info(torrent_id)
    rd_files = info_pre.get("files", [])

    audio_ids = []
    has_archives = False
    for f in rd_files:
        path = f.get("path", "")
        fname = path.rsplit("/", 1)[-1] if "/" in path else path
        if AUDIO_EXTENSIONS.search(fname):
            audio_ids.append(str(f.get("id")))
        elif ARCHIVE_EXTENSIONS.search(fname):
            has_archives = True

    if not audio_ids:
        if has_archives:
            raise _ArchiveOnlyError()
        await client_mod.select_files(torrent_id, "all")
    else:
        await client_mod.select_files(torrent_id, ",".join(audio_ids))

    info = await client_mod.poll_until_ready(torrent_id, interval=2, timeout=120)

    links = info.get("links", [])
    if not links:
        return None

    unrestricted = await asyncio.gather(
        *[client_mod.unrestrict_link(link) for link in links],
        return_exceptions=True,
    )

    tracks = []
    for link, link_url in zip(links, unrestricted):
        if isinstance(link_url, Exception):
            continue
        filename = debrid.link_filename(link, link_url)
        if not AUDIO_EXTENSIONS.search(filename):
            continue
        tracks.append({
            "index": len(tracks),
            "startOffset": 0,
            "duration": 0,
            "title": _clean_filename(filename),
            "contentUrl": link_url,
            "mimeType": _guess_mime(filename),
        })

    return (tracks, torrent_id) if tracks else None


def _serialize_history(h: StreamHistory) -> dict:
    tracks = []
    if h.tracks_json:
        try:
            raw = json.loads(h.tracks_json)
            tracks = tracks_with_stable_urls("h", h.id, raw)
        except Exception:
            pass
    return {
        "id": h.id,
        "title": h.title,
        "author": h.author,
        "coverUrl": h.cover_url,
        "magnetLink": h.magnet_link or "",
        "downloadUrl": h.download_url or "",
        "indexer": h.indexer or "",
        "rdTorrentId": h.rd_torrent_id or "",
        "tracks": tracks,
        "progressSeconds": h.progress_seconds,
        "totalSeconds": h.total_seconds,
        "currentTrackIndex": h.current_track_index,
        "trackPositionSeconds": h.track_position_seconds,
        "hidden": h.hidden,
        "status": h.status,
        "createdAt": h.created_at.isoformat() if h.created_at else "",
        "updatedAt": h.updated_at.isoformat() if h.updated_at else "",
    }
