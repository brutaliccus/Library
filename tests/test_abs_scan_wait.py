"""ABS library scan wait — poll lastScan until scan finishes or times out."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import audiobookshelf as abs_svc


def test_scan_library_and_wait_completes_when_last_scan_changes():
    async def _run():
        lid = "lib_test"
        lib_calls = {"n": 0}
        abs_svc._scan_result_fut = None

        async def fake_get_library(_library_id: str):
            lib_calls["n"] += 1
            # First two polls still old; third has new lastScan
            last = 1000 if lib_calls["n"] < 3 else 2000
            return {"id": lid, "lastScan": last}

        with (
            patch.object(abs_svc, "get_library", side_effect=fake_get_library),
            patch.object(abs_svc, "get_library_item_total", new=AsyncMock(return_value=42)),
            patch.object(abs_svc, "scan_library", new=AsyncMock()) as scan_mock,
            patch.object(abs_svc.asyncio, "sleep", new=AsyncMock()),
        ):
            out = await abs_svc.scan_library_and_wait(
                lid, timeout_seconds=30, poll_interval=0.01
            )

        scan_mock.assert_awaited_once_with(lid)
        assert out["scan_ran"] is True
        assert out["scan_complete"] is True
        assert out["timed_out"] is False
        assert out["items_total"] == 42
        assert out["last_scan"] == 2000
        assert out.get("coalesced") is False

    asyncio.run(_run())


def test_scan_library_and_wait_coalesces_concurrent_callers():
    async def _run():
        lid = "lib_test"
        abs_svc._scan_result_fut = None
        entered_wait = asyncio.Event()
        finish_scan = asyncio.Event()
        sleep_count = {"n": 0}

        async def fake_get_library(_library_id: str):
            last = 2000 if finish_scan.is_set() else 1000
            return {"id": lid, "lastScan": last}

        async def gated_sleep(_t):
            sleep_count["n"] += 1
            if sleep_count["n"] == 1:
                entered_wait.set()
                await finish_scan.wait()
            # Later polls (after lastScan advances) return immediately.

        with (
            patch.object(abs_svc, "get_library", side_effect=fake_get_library),
            patch.object(abs_svc, "get_library_item_total", new=AsyncMock(return_value=7)),
            patch.object(abs_svc, "scan_library", new=AsyncMock()) as scan_mock,
            patch.object(abs_svc.asyncio, "sleep", side_effect=gated_sleep),
        ):
            leader = asyncio.create_task(
                abs_svc.scan_library_and_wait(lid, timeout_seconds=30, poll_interval=0.01)
            )
            await entered_wait.wait()
            assert abs_svc._scan_result_fut is not None
            assert not abs_svc._scan_result_fut.done()
            joiner = asyncio.create_task(
                abs_svc.scan_library_and_wait(lid, timeout_seconds=30, poll_interval=0.01)
            )
            # Real sleep so the joiner attaches before the leader finishes.
            await asyncio.sleep(0.05)
            assert not joiner.done()
            finish_scan.set()
            a, b = await asyncio.gather(leader, joiner)

        scan_mock.assert_awaited_once_with(lid)
        assert a["scan_complete"] is True
        assert b["scan_complete"] is True
        assert {a.get("coalesced"), b.get("coalesced")} == {False, True}

    asyncio.run(_run())


def test_scan_library_and_wait_times_out_with_clear_status():
    async def _run():
        lid = "lib_test"
        abs_svc._scan_result_fut = None

        with (
            patch.object(
                abs_svc,
                "get_library",
                new=AsyncMock(return_value={"id": lid, "lastScan": 1000}),
            ),
            patch.object(abs_svc, "get_library_item_total", new=AsyncMock(return_value=10)),
            patch.object(abs_svc, "scan_library", new=AsyncMock()),
            patch.object(abs_svc.asyncio, "sleep", new=AsyncMock()),
            patch.object(abs_svc.time, "monotonic", side_effect=[0.0, 0.0, 5.0, 5.0]),
        ):
            out = await abs_svc.scan_library_and_wait(
                lid, timeout_seconds=5, poll_interval=0.01
            )

        assert out["scan_ran"] is True
        assert out["scan_complete"] is False
        assert out["timed_out"] is True
        assert out["items_total"] == 10

    asyncio.run(_run())


def test_fix_metadata_uses_deferred_kick_not_full_paginate():
    async def _run():
        with (
            patch.object(abs_svc, "settings") as settings,
            patch.object(
                abs_svc,
                "ensure_metadata_hardening",
                new=AsyncMock(return_value={"library_ok": True, "server_ok": True}),
            ) as harden,
            patch.object(
                abs_svc,
                "kick_library_scan",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "scan_ran": True,
                        "scan_complete": False,
                        "timed_out": False,
                        "deferred": True,
                        "waited_seconds": 0,
                        "items_total": None,
                        "error": None,
                    }
                ),
            ) as kick,
            patch.object(abs_svc, "get_library_item_total", new=AsyncMock(return_value=42)),
            patch.object(
                abs_svc, "_fetch_library_items_all_pages", new=AsyncMock()
            ) as fetch_all,
            patch.object(abs_svc, "update_item_metadata", new=AsyncMock(return_value=True)) as upd,
            patch.object(abs_svc, "invalidate_cache"),
        ):
            settings.abs_library_id = "lib_x"
            settings.abs_api_key = "key"
            out = await abs_svc.fix_metadata_mismatches()

        kick.assert_awaited_once_with(wait=False)
        fetch_all.assert_not_awaited()
        assert out["scan_ran"] is True
        assert out["deferred"] is True
        assert out["items_total"] == 42
        assert out["items_examined"] == 42
        # Must not rewrite LibraForge / Audible titles to bare folder names.
        assert out["count"] == 0
        assert out["fixed"] == []
        upd.assert_not_awaited()
        harden.assert_awaited_once()
        assert out["hardening"]["library_ok"] is True

    asyncio.run(_run())


def test_scan_library_skips_repost_while_inflight():
    async def _run():
        abs_svc._scan_posted_at = None
        abs_svc._scan_posted_before_last = None
        lid = "lib_test"
        post_mock = AsyncMock()
        post_mock.return_value = type("R", (), {"raise_for_status": lambda self: None})()

        with (
            patch.object(
                abs_svc,
                "get_library",
                new=AsyncMock(return_value={"id": lid, "lastScan": 1000}),
            ),
            patch.object(abs_svc, "settings") as settings,
            patch("httpx.AsyncClient") as client_cls,
        ):
            settings.abs_library_id = lid
            settings.abs_url = "http://abs"
            settings.abs_api_key = "k"
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.post = post_mock
            client_cls.return_value = client

            first = await abs_svc.scan_library(lid)
            second = await abs_svc.scan_library(lid)

        assert first is True
        assert second is False
        assert post_mock.await_count == 1

    asyncio.run(_run())
