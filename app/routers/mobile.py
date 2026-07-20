"""Mobile / Android client helpers (APK updates, etc.)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import User
from app.services import github_apk
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/mobile", tags=["mobile"])
logger = logging.getLogger(__name__)


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
    return info
