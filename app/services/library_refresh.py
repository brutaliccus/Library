"""Serialized ABS + Kavita library refresh pipeline.

All refresh entry points (admin Library Refresh, My Library refresh, mobile)
funnel through one coalesced background task so the Pi never runs both full
scans at once — parallel ABS + Kavita scans on a 4 GB host caused OOM freezes.

Order: ABS scan (wait for completion) → orphan cleanup → Kavita scan (wait for
completion) → cache invalidation. Caches are only invalidated after each scan
finishes so collection snapshots are never taken mid-scan (partial data made
the ebook count shrink and bounce back).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.services import audiobookshelf, kavita

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_last_finished_monotonic: float | None = None
_COOLDOWN_SEC = 90.0

_status: dict[str, Any] = {
    "phase": "idle",  # idle | abs | kavita
    "running": False,
    "started_at": None,
    "finished_at": None,
    "abs": None,
    "kavita": None,
    "error": None,
}


def get_status() -> dict[str, Any]:
    """Snapshot of the current/last pipeline run (for frontend polling)."""
    return dict(_status)


def is_running() -> bool:
    return _task is not None and not _task.done()


async def _run_pipeline() -> None:
    global _last_finished_monotonic
    _status.update(
        phase="abs",
        running=True,
        started_at=time.time(),
        finished_at=None,
        abs=None,
        kavita=None,
        error=None,
    )
    try:
        # --- Stage 1: ABS scan + orphan cleanup (blocks until done) ---
        try:
            abs_result = await audiobookshelf.scan_library_and_wait()
            await audiobookshelf.remove_items_with_issues()
            audiobookshelf.invalidate_cache()
            _status["abs"] = {
                "ok": True,
                "scan_complete": bool(abs_result.get("scan_complete")),
                "timed_out": bool(abs_result.get("timed_out")),
                "items_total": abs_result.get("items_total"),
                "waited_seconds": abs_result.get("waited_seconds"),
            }
        except Exception as e:
            logger.warning("Library refresh: ABS stage failed: %s", e)
            _status["abs"] = {"ok": False, "error": str(e)}

        # --- Stage 2: Kavita scan, only after ABS finished (never parallel) ---
        _status["phase"] = "kavita"
        try:
            kv_result = await kavita.scan_library_and_wait()
            _status["kavita"] = {
                "ok": True,
                "scan_complete": bool(kv_result.get("scan_complete")),
                "timed_out": bool(kv_result.get("timed_out")),
                "waited_seconds": kv_result.get("waited_seconds"),
            }
        except Exception as e:
            logger.warning("Library refresh: Kavita stage failed: %s", e)
            _status["kavita"] = {"ok": False, "error": str(e)}
    except Exception as e:  # defensive: never leave status stuck on "running"
        logger.exception("Library refresh pipeline crashed")
        _status["error"] = str(e)
    finally:
        _last_finished_monotonic = time.monotonic()
        _status.update(phase="idle", running=False, finished_at=time.time())
        logger.info(
            "Library refresh pipeline finished (abs=%s kavita=%s)",
            (_status.get("abs") or {}).get("ok"),
            (_status.get("kavita") or {}).get("ok"),
        )


def kick() -> dict[str, Any]:
    """Start (or join) the serialized refresh pipeline. Returns immediately.

    Repeat requests while a run is active coalesce onto it; requests within the
    cooldown window after a finished run are treated as already satisfied.
    """
    global _task
    if is_running():
        return {
            "ok": True,
            "started": False,
            "already_running": True,
            "cooldown": False,
            "message": "Library refresh already running — request coalesced",
        }
    if (
        _last_finished_monotonic is not None
        and (time.monotonic() - _last_finished_monotonic) < _COOLDOWN_SEC
    ):
        return {
            "ok": True,
            "started": False,
            "already_running": False,
            "cooldown": True,
            "message": (
                "Library was refreshed moments ago — skipping to protect the server "
                f"(cooldown {int(_COOLDOWN_SEC)}s)"
            ),
        }
    _task = asyncio.create_task(_run_pipeline())
    return {
        "ok": True,
        "started": True,
        "already_running": False,
        "cooldown": False,
        "message": "Library refresh started — ABS scan first, then Kavita",
    }
