"""Auto-rotate library invite codes on a configurable interval."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session
from app.models import LibraryGroup, _invite_code

logger = logging.getLogger(__name__)

MIN_MINUTES = 60
MAX_MINUTES = 30 * 24 * 60  # 30 days
DEFAULT_MINUTES = 7 * 24 * 60  # 7 days


async def rotation_minutes() -> int:
    try:
        from app.services import instance_settings

        raw = (await instance_settings.get_effective("config.invite_rotation_minutes")).strip()
        n = int(raw) if raw else DEFAULT_MINUTES
    except Exception:
        n = DEFAULT_MINUTES
    return max(MIN_MINUTES, min(MAX_MINUTES, n))


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def rotate_due_invites() -> int:
    """Rotate invite codes older than the configured interval. Returns count."""
    minutes = await rotation_minutes()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rotated = 0
    async with async_session() as db:
        groups = (await db.execute(select(LibraryGroup))).scalars().all()
        for group in groups:
            last = _as_utc(getattr(group, "invite_rotated_at", None)) or _as_utc(group.created_at)
            if last is not None and last > cutoff:
                continue
            group.invite_code = _invite_code()
            group.invite_rotated_at = datetime.now(timezone.utc)
            rotated += 1
            logger.info(
                "Rotated invite code for library group %s (%s)",
                group.id,
                group.name,
            )
        if rotated:
            await db.commit()
    return rotated