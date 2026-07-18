"""Persistent daily shelf snapshots (trending / new releases / home carousels).

In-memory TTL alone meant a quiet overnight + restart left shelves stale until
someone hit the endpoint. Snapshots live in ``app_settings`` so cold starts still
serve yesterday's shelves instantly while a background task refreshes daily.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.services import app_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "shelf_snapshot:"
_DAILY_MAX_AGE_HOURS = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_snapshot(name: str) -> dict[str, Any] | None:
    raw = await app_settings.get_setting(f"{_KEY_PREFIX}{name}", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "payload" not in data:
        return None
    return data


async def put_snapshot(name: str, payload: Any) -> None:
    envelope = {
        "refreshedAt": _utcnow().isoformat(),
        "payload": payload,
    }
    await app_settings.set_setting(f"{_KEY_PREFIX}{name}", json.dumps(envelope, default=str))


def snapshot_age_hours(envelope: dict[str, Any] | None) -> float | None:
    if not envelope:
        return None
    raw = envelope.get("refreshedAt") or ""
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (_utcnow() - ts).total_seconds() / 3600.0


def is_fresh(envelope: dict[str, Any] | None, *, max_age_hours: float = _DAILY_MAX_AGE_HOURS) -> bool:
    age = snapshot_age_hours(envelope)
    if age is None:
        return False
    return age < max_age_hours


def same_utc_day(envelope: dict[str, Any] | None) -> bool:
    if not envelope:
        return False
    raw = envelope.get("refreshedAt") or ""
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.date() == _utcnow().date()
