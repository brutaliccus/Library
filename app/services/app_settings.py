"""Generic accessor for admin-tunable key/value settings.

Values live in the ``app_settings`` table (see :class:`app.models.AppSetting`)
so the admin console can change them at runtime without a redeploy. Unlike the
scraper-specific helpers, this is a plain string store used for things like
third-party API keys that default to their env var when unset.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.database import async_session
from app.models import AppSetting

logger = logging.getLogger(__name__)


async def get_setting(key: str, default: str = "") -> str:
    """Return the stored string for ``key``, or ``default`` when unset/empty."""
    try:
        async with async_session() as db:
            row = (
                await db.execute(select(AppSetting).where(AppSetting.key == key))
            ).scalar_one_or_none()
            if row and row.value:
                return row.value
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("get_setting(%s) failed: %s", key, e)
    return default


async def set_setting(key: str, value: str) -> None:
    """Upsert ``key`` -> ``value``. An empty value deletes the override."""
    from app.database import run_with_sqlite_retry

    async def _do() -> None:
        async with async_session() as db:
            row = (
                await db.execute(select(AppSetting).where(AppSetting.key == key))
            ).scalar_one_or_none()
            if value:
                if row:
                    row.value = value
                else:
                    db.add(AppSetting(key=key, value=value))
            elif row:
                await db.delete(row)
            await db.commit()

    await run_with_sqlite_retry(_do, attempts=6, base_delay=0.5)
