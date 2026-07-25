"""Web Push notifications for download completion and admin alerts."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import PushSubscription, User

logger = logging.getLogger(__name__)


def _settings():
    # Resolve at call time so Admin Config / runtime overrides are visible.
    return get_settings()


def _send_one(subscription_info: dict, data: str, vapid_instance, vapid_claims: dict) -> None:
    from pywebpush import webpush
    webpush(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=vapid_instance,
        vapid_claims=vapid_claims,
    )


def _response_status(exc: BaseException) -> int | None:
    """Extract HTTP status from WebPushException without truthiness traps.

    ``requests.Response`` is falsy for 4xx/5xx (``__bool__`` → ``self.ok``), so
    ``if e.response and e.response.status_code == 410`` never matches expired
    subscriptions — they were logged as generic failures and left in the DB.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    code = getattr(resp, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _is_gone_subscription(exc: BaseException) -> bool:
    code = _response_status(exc)
    if code in (404, 410):
        return True
    msg = str(exc).lower()
    return "410" in msg or "404" in msg or "unsubscribed" in msg or "expired" in msg


def _vapid_instance(private_key: str):
    from pathlib import Path
    from py_vapid import Vapid

    if "-----BEGIN" in private_key:
        pem = private_key.replace("\\n", "\n")
        return Vapid.from_pem(pem.encode())
    path = Path(private_key)
    if path.is_file():
        return Vapid.from_file(private_key_file=str(path))
    # Raw URL-safe base64 private key (some setups store it this way).
    return private_key


async def send_push_to_user(db: AsyncSession, user_id: int, payload: dict[str, Any]) -> None:
    """Send a push notification to all subscriptions for a user."""
    settings = _settings()
    if not settings.vapid_private_key:
        logger.info("Push skipped: VAPID key not configured")
        return

    try:
        import pywebpush  # noqa: F401
    except ImportError:
        logger.warning("pywebpush not installed, skipping push")
        return

    result = await db.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    subs = list(result.scalars().all())
    if not subs:
        logger.info(
            "Push skipped for user %s: no subscriptions (enable push on Admin or My Requests page)",
            user_id,
        )
        return

    data = json.dumps(payload)
    domain = settings.app_url.replace("https://", "").replace("http://", "").split("/")[0]
    vapid_claims = {"sub": f"mailto:admin@{domain}"}

    try:
        vapid_instance = _vapid_instance(settings.vapid_private_key)
    except Exception:
        logger.exception("Push skipped: invalid VAPID private key")
        return

    expired_ids: list[int] = []
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
            logger.info("Push sent to user %s (sub %s)", user_id, sub.id)
        except Exception as e:
            if _is_gone_subscription(e):
                logger.info(
                    "Push subscription expired for user %s (sub %s) — removing",
                    user_id,
                    sub.id,
                )
                expired_ids.append(sub.id)
            else:
                logger.warning(
                    "Push failed for user %s (sub %s): %s",
                    user_id,
                    sub.id,
                    e,
                )

    if expired_ids:
        for sub in subs:
            if sub.id in expired_ids:
                await db.delete(sub)
        await db.commit()
        logger.info(
            "Removed %d expired push subscription(s) for user %s",
            len(expired_ids),
            user_id,
        )


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
    """Send push (+ live WS for native LocalNotifications) to all admins."""
    result = await db.execute(select(User.id).where(User.role == "admin"))
    admin_ids = [r[0] for r in result.fetchall()]
    logger.info("Notifying %d admin(s) for: %s", len(admin_ids), payload.get("title", "?"))
    for admin_id in admin_ids:
        await send_push_to_user(db, admin_id, payload)

    # Capacitor APK cannot use Web Push; fan out over WS so open native clients
    # can surface LocalNotifications (useNativeNotifications listens for these).
    try:
        from app.utils.websocket import ws_manager

        ws_payload = {
            "type": "admin_alert",
            "title": payload.get("title") or "Library",
            "detail": payload.get("body") or "",
            "url": payload.get("url") or "/admin",
            "alert_type": payload.get("type") or "admin_alert",
        }
        for admin_id in admin_ids:
            await ws_manager.send_to_user(admin_id, ws_payload)
    except Exception:
        logger.debug("Admin WS alert fanout failed", exc_info=True)


async def notify_admins_background(payload: dict[str, Any]) -> None:
    """Notify admins from a background task (creates its own db session)."""
    from app.database import async_session
    try:
        async with async_session() as db:
            await notify_admins(db, payload)
        logger.info("Admin push sent: %s", payload.get("title", "?"))
    except Exception as e:
        logger.exception("Admin push failed: %s", e)


async def notify_request_failed(
    db: AsyncSession,
    *,
    user_id: int,
    title: str,
    detail: str,
    notify_admins_too: bool = True,
    username: str | None = None,
) -> None:
    """Notify the requesting user (and optionally admins) that a request failed."""
    body = (detail or "Request failed")[:300]
    try:
        await send_push_to_user(
            db,
            user_id,
            {
                "type": "download_failed",
                "title": f"{title} failed",
                "body": body,
                "url": "/requests",
            },
        )
    except Exception:
        logger.warning("User failure push failed", exc_info=True)

    if notify_admins_too:
        who = username or f"user #{user_id}"
        try:
            await notify_admins(
                db,
                {
                    "type": "download_failed",
                    "title": "Download Failed",
                    "body": f"{title} (requested by {who}): {body[:200]}",
                    "url": "/admin?tab=requests",
                },
            )
        except Exception:
            logger.warning("Admin failure push failed", exc_info=True)
