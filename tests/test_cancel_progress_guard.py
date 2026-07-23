"""Cancel must stay terminal — progress writers must not revive requests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import pipeline
from app.services.libraforge import LibraForgeError, wait_for_run


def _session_returning(row):
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: row)
    )
    db.commit = AsyncMock()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return db, session_cm


def test_report_progress_skips_cancelled_request():
    """In-flight progress must not WS/DB-overwrite a cancelled request."""

    async def _run():
        cancelled_row = SimpleNamespace(status="cancelled")
        db, session_cm = _session_returning(cancelled_row)

        with (
            patch.object(pipeline.ws_manager, "send_to_user", new=AsyncMock()) as send,
            patch.object(pipeline, "async_session", return_value=session_cm),
            patch.object(pipeline, "_progress_db_throttle", {}),
        ):
            await pipeline._report_progress(
                42,
                7,
                "downloading_rd",
                "Real-Debrid downloading… 50%",
                progress_percent=50.0,
            )
            send.assert_not_awaited()
            db.commit.assert_not_awaited()

    asyncio.run(_run())


def test_report_progress_writes_when_active():
    async def _run():
        active = SimpleNamespace(
            status="downloading_rd",
            status_detail="old",
            progress_percent=None,
            progress_bytes=None,
            progress_total_bytes=None,
            progress_speed_bps=None,
        )
        db, session_cm = _session_returning(active)

        with (
            patch.object(pipeline.ws_manager, "send_to_user", new=AsyncMock()) as send,
            patch.object(pipeline, "async_session", return_value=session_cm),
            patch.object(pipeline, "_progress_db_throttle", {}),
        ):
            await pipeline._report_progress(
                42,
                7,
                "downloading_rd",
                "Real-Debrid downloading… 50%",
                progress_percent=50.0,
            )
            send.assert_awaited_once()
            db.commit.assert_awaited_once()
            assert active.status == "downloading_rd"
            assert active.progress_percent == 50.0

    asyncio.run(_run())


def test_wait_for_run_aborts_when_should_abort():
    async def _run():
        with (
            patch("app.services.libraforge.get_run", new=AsyncMock()) as get_run,
            patch("app.services.libraforge.cancel_run", new=AsyncMock()) as cancel_run,
        ):
            with pytest.raises(LibraForgeError, match="cancelled"):
                await wait_for_run(
                    "run-1",
                    poll_seconds=0.01,
                    should_abort=lambda: True,
                )
            cancel_run.assert_awaited_once_with("run-1")
            get_run.assert_not_awaited()

    asyncio.run(_run())
