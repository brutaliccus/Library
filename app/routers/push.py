"""Web Push subscription API."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import PushSubscription, User
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/push", tags=["push"])
settings = get_settings()


class SubscribeRequest(BaseModel):
    endpoint: str
    keys: dict  # {"p256dh": str, "auth": str}


class VapidPublicResponse(BaseModel):
    publicKey: str


@router.get("/vapid-public", response_model=VapidPublicResponse)
async def get_vapid_public():
    """Return the VAPID public key for the client to subscribe."""
    if not settings.vapid_public_key:
        raise HTTPException(status_code=503, detail="Push notifications not configured")
    return VapidPublicResponse(publicKey=settings.vapid_public_key)


@router.post("/subscribe")
async def subscribe(
    body: SubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a push subscription for the current user."""
    if not settings.vapid_private_key:
        raise HTTPException(status_code=503, detail="Push notifications not configured")

    keys = body.keys or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Missing p256dh or auth key")

    result = await db.execute(
        select(PushSubscription).where(
            PushSubscription.user_id == user.id,
            PushSubscription.endpoint == body.endpoint,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.p256dh = p256dh
        existing.auth = auth
    else:
        sub = PushSubscription(
            user_id=user.id,
            endpoint=body.endpoint,
            p256dh=p256dh,
            auth=auth,
        )
        db.add(sub)
    await db.commit()
    return {"ok": True}


@router.delete("/subscribe")
async def unsubscribe(
    endpoint: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a push subscription."""
    result = await db.execute(
        select(PushSubscription).where(
            PushSubscription.user_id == user.id,
            PushSubscription.endpoint == endpoint,
        )
    )
    sub = result.scalar_one_or_none()
    if sub:
        await db.delete(sub)
        await db.commit()
    return {"ok": True}
