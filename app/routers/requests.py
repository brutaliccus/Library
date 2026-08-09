import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.database import async_session, get_db
from app.models import User, DownloadRequest
from app.utils.auth import get_current_user
from app.utils.websocket import ws_manager
from app.services import google_books
from app.services.pipeline import process_download, process_aa_download

router = APIRouter(prefix="/api/requests", tags=["requests"])
settings = get_settings()
logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({
    "pending",
    "sent_to_rd",
    "downloading_rd",
    "transferring",
    "organizing",
    "metadata_forge",
    "m4b_convert",
    "chapter_forge",
    "folder_forge",
    "finalizing",
})
_RETRYABLE_STATUSES = frozenset({"failed", "cancelled", "admin_rejected", "skipped"})
_COVER_BACKFILL_LIMIT = 24
# Persisted when a lookup found nothing — avoids retrying the same rows on every list.
_COVER_NONE_SENTINEL = "-"
_cover_backfill_tasks: set[asyncio.Task] = set()


class CreateDownloadRequest(BaseModel):
    title: str
    author: str | None = None
    magnet_link: str | None = None
    download_url: str | None = None
    indexer: str | None = None
    size_bytes: int | None = None
    media_type: str = "audiobook"
    source: str | None = None
    aa_md5: str | None = None
    aa_file_extension: str | None = None
    google_volume_id: str | None = None
    catalog_title: str | None = None
    cover_url: str | None = None
    # Optional ABB / torrent file list for multi-book pack splitting
    release_files: list[dict] | list[str] | None = None


class DownloadRequestResponse(BaseModel):
    id: int
    title: str
    author: str | None
    media_type: str
    status: str
    status_detail: str | None
    size_bytes: int | None
    indexer: str | None
    is_private: bool = False
    google_volume_id: str | None = None
    cover_url: str | None = None
    created_at: str
    completed_at: str | None
    progress_percent: float | None = None
    progress_bytes: int | None = None
    progress_total_bytes: int | None = None
    progress_speed_bps: float | None = None
    staging_path: str | None = None
    quarantine_reason: str | None = None
    manual_review_url: str | None = None

    model_config = {"from_attributes": True}


@router.post("", response_model=DownloadRequestResponse)
async def create_request(
    body: CreateDownloadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    is_aa = body.source == "annas_archive" and body.aa_md5
    link = body.magnet_link or body.download_url

    if not is_aa and not link:
        raise HTTPException(status_code=400, detail="Either magnet_link or download_url is required")

    stored_title = (body.catalog_title or body.title or "").strip() or body.title
    volume_id = (body.google_volume_id or "").strip() or None
    cover_url = (body.cover_url or "").strip() or None
    if not cover_url:
        try:
            cover_url = (
                await google_books.lookup_cover_url(
                    volume_id, stored_title, body.author or ""
                )
            ).strip() or None
        except Exception:
            logger.debug("cover lookup on create failed for %s", stored_title, exc_info=True)
            cover_url = None
    if cover_url:
        cover_url = cover_url[:1024]

    release_files_json = None
    if body.release_files:
        try:
            from app.services.release_files import dumps_release_files

            release_files_json = dumps_release_files(body.release_files)
        except Exception:
            release_files_json = None

    dl_request = DownloadRequest(
        user_id=user.id,
        title=stored_title,
        author=body.author,
        magnet_link=link or f"aa:{body.aa_md5}",
        indexer=body.indexer or ("Anna's Archive" if is_aa else None),
        size_bytes=body.size_bytes,
        media_type=body.media_type,
        rd_torrent_id=body.aa_md5 if is_aa else None,
        aa_file_extension=body.aa_file_extension if is_aa else None,
        is_private=user.private_mode,
        google_volume_id=volume_id,
        cover_url=cover_url,
        release_files_json=release_files_json,
    )
    db.add(dl_request)
    # Commit before scheduling background work so the worker session can see the row
    # (same pattern as retry_request). Flush-only races leave process_* looking up a
    # missing request id.
    await db.commit()
    await db.refresh(dl_request)

    if is_aa:
        asyncio.create_task(process_aa_download(dl_request.id))
    else:
        asyncio.create_task(process_download(dl_request.id))

    return _to_response(dl_request)


@router.get("", response_model=list[DownloadRequestResponse])
async def list_my_requests(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DownloadRequest)
        .where(DownloadRequest.user_id == user.id)
        .order_by(DownloadRequest.created_at.desc())
    )
    rows = list(result.scalars().all())
    # Never block the list on external cover APIs — fill in the background.
    _schedule_cover_backfill([r.id for r in rows if _needs_cover_backfill(r)])
    return [_to_response(r) for r in rows]


@router.get("/{request_id}", response_model=DownloadRequestResponse)
async def get_request(
    request_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await _get_user_request(request_id, user.id, db)
    return _to_response(req)


@router.post("/{request_id}/cancel", response_model=DownloadRequestResponse)
async def cancel_request(
    request_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await _get_user_request(request_id, user.id, db)
    if req.status not in _ACTIVE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot cancel request in status '{req.status}'")
    forge_run_id = (getattr(req, "libraforge_run_id", None) or "").strip() or None
    req.status = "cancelled"
    req.status_detail = "Cancelled by user"
    req.progress_percent = None
    req.progress_bytes = None
    req.progress_total_bytes = None
    req.progress_speed_bps = None
    await db.commit()
    await db.refresh(req)
    await ws_manager.send_to_user(
        user.id,
        {
            "type": "status_update",
            "request_id": req.id,
            "status": req.status,
            "detail": req.status_detail,
            "progress_percent": None,
            "progress_bytes": None,
            "progress_total_bytes": None,
            "progress_speed_bps": None,
        },
    )
    if forge_run_id:
        try:
            from app.services import libraforge

            await libraforge.cancel_run(forge_run_id)
        except Exception:
            logger.debug("LibraForge cancel_run for request %s failed", request_id, exc_info=True)
    return _to_response(req)


@router.post("/{request_id}/retry", response_model=DownloadRequestResponse)
async def retry_request(
    request_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await _get_user_request(request_id, user.id, db)
    if req.status not in _RETRYABLE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot retry request in status '{req.status}'")

    from app.services import library_ingest

    # Sweep / owned upload — never Real-Debrid; re-kick forge from staging.
    if library_ingest.is_local_ingest_request(req):
        req.status = "pending"
        req.status_detail = "Retrying local ingest…"
        req.completed_at = None
        req.progress_percent = None
        req.progress_bytes = None
        req.progress_total_bytes = None
        req.progress_speed_bps = None
        await db.commit()
        await db.refresh(req)

        async def _retry_local() -> None:
            try:
                await library_ingest.reprocess_local_ingest(request_id)
            except Exception:
                logger.exception("Local ingest retry failed for request %s", request_id)

        asyncio.create_task(_retry_local())
        await ws_manager.send_to_user(
            user.id,
            {
                "type": "status_update",
                "request_id": req.id,
                "status": req.status,
                "detail": req.status_detail,
            },
        )
        return _to_response(req)

    is_aa = (req.magnet_link or "").startswith("aa:") or (
        (req.indexer or "").lower().find("anna") >= 0 and bool(req.rd_torrent_id)
    )
    req.status = "pending"
    req.status_detail = "Retrying…"
    req.completed_at = None
    req.progress_percent = None
    req.progress_bytes = None
    req.progress_total_bytes = None
    req.progress_speed_bps = None
    # Non-AA: clear prior debrid binding so retry re-applies preferred/cache pick,
    # but exclude the provider that already failed (unique-cache would otherwise
    # re-select TorBox forever).
    if not is_aa:
        from app.services.pipeline import set_retry_exclude_providers

        failed_provider = getattr(req, "debrid_provider", None)
        if failed_provider:
            set_retry_exclude_providers(req.id, [failed_provider])
        req.rd_torrent_id = None
        req.debrid_provider = None
    await db.commit()
    await db.refresh(req)

    if is_aa:
        asyncio.create_task(process_aa_download(req.id))
    else:
        asyncio.create_task(process_download(req.id))

    await ws_manager.send_to_user(
        user.id,
        {
            "type": "status_update",
            "request_id": req.id,
            "status": req.status,
            "detail": req.status_detail,
        },
    )
    return _to_response(req)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        await websocket.close(code=1008, reason="Invalid token")
        return

    await ws_manager.connect(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, user_id)


async def _get_user_request(request_id: int, user_id: int, db: AsyncSession) -> DownloadRequest:
    result = await db.execute(
        select(DownloadRequest).where(
            DownloadRequest.id == request_id,
            DownloadRequest.user_id == user_id,
        )
    )
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    return req


def _needs_cover_backfill(req: DownloadRequest) -> bool:
    cover = (getattr(req, "cover_url", None) or "").strip()
    return not cover


def _schedule_cover_backfill(request_ids: list[int]) -> None:
    ids = [i for i in request_ids if i][:_COVER_BACKFILL_LIMIT]
    if not ids:
        return
    task = asyncio.create_task(_backfill_request_covers_bg(ids))
    _cover_backfill_tasks.add(task)
    task.add_done_callback(_cover_backfill_tasks.discard)


async def _backfill_request_covers_bg(request_ids: list[int]) -> None:
    """Best-effort cover fill in a fresh DB session (does not block list endpoints)."""
    try:
        async with async_session() as db:
            result = await db.execute(
                select(DownloadRequest).where(DownloadRequest.id.in_(request_ids))
            )
            rows = [r for r in result.scalars().all() if _needs_cover_backfill(r)]
            if not rows:
                return

            async def _fill(req: DownloadRequest) -> None:
                try:
                    cover = await asyncio.wait_for(
                        google_books.lookup_cover_url(
                            getattr(req, "google_volume_id", None),
                            req.title or "",
                            req.author or "",
                        ),
                        timeout=8.0,
                    )
                except Exception:
                    cover = ""
                # Sentinel stops infinite retries when every provider returns empty.
                req.cover_url = (cover or _COVER_NONE_SENTINEL)[:1024]

            await asyncio.gather(*[_fill(r) for r in rows])
            await db.commit()
    except Exception:
        logger.debug("Background request cover backfill failed", exc_info=True)


def _response_cover_url(req: DownloadRequest) -> str | None:
    cover = (getattr(req, "cover_url", None) or "").strip()
    if not cover or cover == _COVER_NONE_SENTINEL:
        return None
    return cover


def _to_response(req: DownloadRequest) -> DownloadRequestResponse:
    from app.services import libraforge

    review_url = None
    # LibraForge Manual Review is audiobook-only.
    if req.status == "quarantined" and (req.media_type or "") != "ebook":
        review_url = libraforge.public_manual_review_url() or None
    return DownloadRequestResponse(
        id=req.id,
        title=req.title,
        author=req.author,
        media_type=req.media_type or "unknown",
        status=req.status,
        status_detail=req.status_detail,
        size_bytes=req.size_bytes,
        indexer=req.indexer,
        is_private=bool(req.is_private),
        google_volume_id=getattr(req, "google_volume_id", None),
        cover_url=_response_cover_url(req),
        created_at=req.created_at.isoformat() if req.created_at else "",
        completed_at=req.completed_at.isoformat() if req.completed_at else None,
        progress_percent=req.progress_percent,
        progress_bytes=req.progress_bytes,
        progress_total_bytes=req.progress_total_bytes,
        progress_speed_bps=req.progress_speed_bps,
        staging_path=getattr(req, "staging_path", None),
        quarantine_reason=getattr(req, "quarantine_reason", None),
        manual_review_url=review_url,
    )
