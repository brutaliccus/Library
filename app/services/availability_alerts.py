"""Watchlist for catalog books not yet present in the indexer cache."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models import AvailabilityAlert
from app.services import indexer_cache, push

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_alert(user_id: int, volume_id: str) -> AvailabilityAlert | None:
    async with async_session() as db:
        return (
            await db.execute(
                select(AvailabilityAlert).where(
                    AvailabilityAlert.user_id == user_id,
                    AvailabilityAlert.google_volume_id == volume_id,
                    AvailabilityAlert.notified_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def list_alerts(user_id: int) -> list[AvailabilityAlert]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AvailabilityAlert)
                .where(
                    AvailabilityAlert.user_id == user_id,
                    AvailabilityAlert.notified_at.is_(None),
                )
                .order_by(AvailabilityAlert.created_at.desc())
            )
        ).scalars().all()
        return list(rows)


async def create_alert(
    user_id: int,
    volume_id: str,
    *,
    title: str = "",
    author: str = "",
    cover_url: str = "",
) -> AvailabilityAlert:
    async with async_session() as db:
        existing = (
            await db.execute(
                select(AvailabilityAlert).where(
                    AvailabilityAlert.user_id == user_id,
                    AvailabilityAlert.google_volume_id == volume_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.notified_at = None
            existing.title = (title or existing.title)[:512]
            existing.author = (author or existing.author)[:256]
            existing.cover_url = (cover_url or existing.cover_url)[:1024]
            await db.commit()
            await db.refresh(existing)
            return existing

        row = AvailabilityAlert(
            user_id=user_id,
            google_volume_id=volume_id[:64],
            title=(title or "")[:512],
            author=(author or "")[:256],
            cover_url=(cover_url or "")[:1024],
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def delete_alert(user_id: int, volume_id: str) -> bool:
    async with async_session() as db:
        row = (
            await db.execute(
                select(AvailabilityAlert).where(
                    AvailabilityAlert.user_id == user_id,
                    AvailabilityAlert.google_volume_id == volume_id,
                )
            )
        ).scalar_one_or_none()
        if not row:
            return False
        await db.delete(row)
        await db.commit()
        return True


async def notify_fulfilled_alerts() -> int:
    """Push users whose watched books now have exact/likely cache matches.

    Called after scraper catalog-match passes (RSS / keyword ingest).
    """
    async with async_session() as db:
        pending = (
            await db.execute(
                select(AvailabilityAlert).where(AvailabilityAlert.notified_at.is_(None))
            )
        ).scalars().all()
        pending_rows = list(pending)

    if not pending_rows:
        return 0

    volume_ids = list({r.google_volume_id for r in pending_rows})
    available = await indexer_cache.volume_ids_with_matches(volume_ids)
    if not available:
        return 0

    notified = 0
    now = _utcnow()
    async with async_session() as db:
        for row in pending_rows:
            info = available.get(row.google_volume_id)
            if not info or not info.get("available"):
                continue
            live = (
                await db.execute(
                    select(AvailabilityAlert).where(AvailabilityAlert.id == row.id)
                )
            ).scalar_one_or_none()
            if not live or live.notified_at is not None:
                continue
            live.notified_at = now
            title = live.title or "A book you watchlisted"
            book_url = f"/book/{live.google_volume_id}"
            try:
                await push.send_push_to_user(
                    db,
                    live.user_id,
                    {
                        "type": "availability_alert",
                        "title": f"{title} is available",
                        "body": "It's now in the download cache — tap to open",
                        "url": book_url,
                    },
                )
                notified += 1
            except Exception as e:
                logger.warning(
                    "Availability alert push failed user=%s vol=%s: %s",
                    live.user_id,
                    live.google_volume_id,
                    e,
                )
        await db.commit()

    if notified:
        logger.info("Availability alerts notified: %s", notified)
    return notified
