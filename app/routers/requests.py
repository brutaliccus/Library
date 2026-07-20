import asyncio

from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.database import get_db
from app.models import User, DownloadRequest
from app.utils.auth import get_current_user
from app.utils.websocket import ws_manager
from app.services.pipeline import process_download, process_aa_download

router = APIRouter(prefix="/api/requests", tags=["requests"])
settings = get_settings()

_ACTIVE_STATUSES = frozenset({
    "pending",
    "sent_to_rd",
    "downloading_rd",
    "transferring",
    "organizing",
})
_RETRYABLE_STATUSES = frozenset({"failed", "cancelled"})


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
        google_volume_id=(body.google_volume_id or "").strip() or None,
        cover_url=(body.cover_url or "").strip() or None,
    )
    db.add(dl_request)
    await db.flush()
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
    return [_to_response(r) for r in result.scalars().all()]


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
        },
    )
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


def _to_response(req: DownloadRequest) -> DownloadRequestResponse:
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
        cover_url=getattr(req, "cover_url", None),
        created_at=req.created_at.isoformat() if req.created_at else "",
        completed_at=req.completed_at.isoformat() if req.completed_at else None,
        progress_percent=req.progress_percent,
        progress_bytes=req.progress_bytes,
        progress_total_bytes=req.progress_total_bytes,
        progress_speed_bps=req.progress_speed_bps,
    )
