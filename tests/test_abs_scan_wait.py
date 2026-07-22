"""ABS library scan wait — poll lastScan until scan finishes or times out."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import audiobookshelf as abs_svc


def test_scan_library_and_wait_completes_when_last_scan_changes():
    async def _run():
        lid = "lib_test"
        lib_calls = {"n": 0}

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

    asyncio.run(_run())


def test_scan_library_and_wait_times_out_with_clear_status():
    async def _run():
        lid = "lib_test"

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


def test_fix_metadata_waits_for_scan_before_fetching_items():
    async def _run():
        order: list[str] = []

        async def wait_scan(_lid=None, **_kwargs):
            order.append("wait")
            return {
                "scan_ran": True,
                "scan_complete": True,
                "timed_out": False,
                "items_total": 2,
                "waited_seconds": 1.0,
                "last_scan": 99,
            }

        async def fetch_items(_lid):
            order.append("fetch")
            return [
                {
                    "id": "i1",
                    "relPath": "Author/Book One",
                    "media": {"metadata": {"title": "Wrong"}},
                }
            ]

        with (
            patch.object(abs_svc, "settings") as settings,
            patch.object(abs_svc, "scan_library_and_wait", side_effect=wait_scan),
            patch.object(abs_svc, "remove_items_with_issues", new=AsyncMock(return_value=True)),
            patch.object(abs_svc, "_fetch_library_items_all_pages", side_effect=fetch_items),
            patch.object(abs_svc, "update_item_metadata", new=AsyncMock(return_value=True)),
            patch.object(abs_svc, "invalidate_cache"),
        ):
            settings.abs_library_id = "lib_x"
            settings.abs_api_key = "key"
            out = await abs_svc.fix_metadata_mismatches()

        assert order == ["wait", "fetch"]
        assert out["scan_ran"] is True
        assert out["scan_complete"] is True
        assert out["timed_out"] is False
        assert out["count"] == 1
        assert out["fixed"][0]["newTitle"] == "Book One"

    asyncio.run(_run())
