"""Mobile / Android client helpers (APK updates, etc.)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import User
from app.services import github_apk, instance_settings
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/mobile", tags=["mobile"])
logger = logging.getLogger(__name__)


async def _android_update_policy() -> tuple[int, bool]:
    """Return (minVersionCode, forceUpdates) from Admin / env defaults."""
    raw_min = (await instance_settings.get_effective("config.android_min_version_code") or "").strip()
    try:
        min_code = int(raw_min) if raw_min else 56
    except ValueError:
        min_code = 56
    force = await instance_settings.get_effective_bool("config.android_force_updates", True)
    return max(0, min_code), force


@router.get("/android-update")
async def android_update(
    force: bool = Query(False),
    _user: User = Depends(get_current_user),
):
    """Latest Library APK on GitHub Releases for the Android app updater."""
    try:
        info = await github_apk.fetch_latest_android_apk(force=force)
    except Exception as e:
        logger.warning("android-update lookup failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach GitHub Releases") from e
    if not info:
        raise HTTPException(
            status_code=404,
            detail="No Android APK release found on GitHub yet",
        )

    min_code, force_updates = await _android_update_policy()
    # Include policy in releaseKey so old clients that dismissed a soft prompt
    # re-prompt when force/min settings change (dismissed key no longer matches).
    base_key = str(info.get("releaseKey") or "")
    policy_key = f"{base_key}|min:{min_code}|force:{1 if force_updates else 0}"
    return {
        **info,
        "releaseKey": policy_key,
        "minVersionCode": min_code,
        "forceUpdate": force_updates,
    }
