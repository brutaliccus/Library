import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User, DownloadRequest
from app.utils.auth import get_current_user
from app.utils.websocket import ws_manager
from app.services.pipeline import process_download, process_aa_download
from app.routers.library import add_to_library_from_stream

router = APIRouter(prefix="/api/requests", tags=["requests"])
settings = get_settings()


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
    # Catalog volume the user was viewing (Personal Collection + in-library badges)
    google_volume_id: str | None = None
    catalog_title: str | None = None


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

    # Prefer catalog title for matching/hide so ABS clean titles line up with requests.
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
    )
    db.add(dl_request)
    await db.flush()
    await db.refresh(dl_request)

    # Always add to Personal Collection for the requester (private or not) so they
    # can find the book again from the catalog volume page.
    try:
        await add_to_library_from_stream(
            user.id,
            body.catalog_title or body.title,
            body.author or "",
            magnet_link=link if (link or "").startswith("magnet:") else None,
            google_volume_id=body.google_volume_id,
            db=db,
        )
    except Exception:
        pass  # Non-fatal; request still succeeds

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
    result = await db.execute(
        select(DownloadRequest).where(
            DownloadRequest.id == request_id,
            DownloadRequest.user_id == user.id,
        )
    )
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
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
        created_at=req.created_at.isoformat() if req.created_at else "",
        completed_at=req.completed_at.isoformat() if req.completed_at else None,
        progress_percent=req.progress_percent,
        progress_bytes=req.progress_bytes,
        progress_total_bytes=req.progress_total_bytes,
        progress_speed_bps=req.progress_speed_bps,
    )
