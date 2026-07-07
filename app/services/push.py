"""Web Push notifications for download completion and admin alerts."""
import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import PushSubscription, User

logger = logging.getLogger(__name__)
settings = get_settings()


def _send_one(subscription_info: dict, data: str, vapid_instance, vapid_claims: dict) -> None:
    from pywebpush import webpush, WebPushException
    webpush(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=vapid_instance,
        vapid_claims=vapid_claims,
    )


async def send_push_to_user(db: AsyncSession, user_id: int, payload: dict[str, Any]) -> None:
    """Send a push notification to all subscriptions for a user."""
    if not settings.vapid_private_key:
        logger.info("Push skipped: VAPID key not configured")
        return

    try:
        from pywebpush import WebPushException
    except ImportError:
        logger.warning("pywebpush not installed, skipping push")
        return

    result = await db.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    subs = result.scalars().all()
    if not subs:
        logger.info("Push skipped for user %s: no subscriptions (enable push on Admin or My Requests page)", user_id)
        return

    data = json.dumps(payload)
    domain = settings.app_url.replace("https://", "").replace("http://", "").split("/")[0]
    vapid_claims = {"sub": f"mailto:admin@{domain}"}

    vapid_key = settings.vapid_private_key
    if isinstance(vapid_key, str) and "-----BEGIN" in vapid_key:
        vapid_key = vapid_key.replace("\\n", "\n")
        from py_vapid import Vapid
        vapid_instance = Vapid.from_pem(vapid_key.encode())
    elif isinstance(vapid_key, str):
        from py_vapid import Vapid
        vapid_instance = Vapid.from_file(private_key_file=vapid_key)
    else:
        vapid_instance = vapid_key

    for sub in subs:
        try:
            subscription_info = {
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            }
            await asyncio.to_thread(
                _send_one,
                subscription_info,
                data,
                vapid_instance,
                vapid_claims,
            )
            logger.info("Push sent to user %s", user_id)
        except Exception as e:
            if hasattr(e, "response") and e.response and e.response.status_code in (404, 410):
                logger.info("Push subscription expired for user %s", user_id)
            else:
                logger.warning("Push failed for user %s: %s", user_id, e)


async def notify_download_complete(user_id: int, title: str, lib_name: str, db: AsyncSession) -> None:
    """Send push notification when a requested book is ready."""
    await send_push_to_user(
        db,
        user_id,
        {
            "type": "download_complete",
            "title": f"{title} is ready",
            "body": f"Available in {lib_name}",
            "url": "/my-library",
        },
    )


async def notify_admins(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Send push notification to all admin users who have subscriptions."""
    result = await db.execute(select(User.id).where(User.role == "admin"))
    admin_ids = [r[0] for r in result.fetchall()]
    logger.info("Notifying %d admin(s) for: %s", len(admin_ids), payload.get("title", "?"))
    for admin_id in admin_ids:
        await send_push_to_user(db, admin_id, payload)


async def notify_admins_background(payload: dict[str, Any]) -> None:
    """Notify admins from a background task (creates its own db session)."""
    from app.database import async_session
    try:
        async with async_session() as db:
            await notify_admins(db, payload)
        logger.info("Admin push sent: %s", payload.get("title", "?"))
    except Exception as e:
        logger.exception("Admin push failed: %s", e)
