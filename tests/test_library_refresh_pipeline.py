"""Serialized ABS -> Kavita refresh pipeline + Kavita scan-wait / shrink guard."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import kavita as kavita_svc
from app.services import library_refresh as pipeline


def _reset_pipeline():
    pipeline._task = None
    pipeline._last_finished_monotonic = None
    pipeline._status.update(
        phase="idle", running=False, started_at=None, finished_at=None,
        abs=None, kavita=None, error=None,
    )


def test_pipeline_runs_abs_then_kavita_serially():
    async def _run():
        _reset_pipeline()
        order: list[str] = []

        async def fake_abs_wait():
            order.append("abs-start")
            await asyncio.sleep(0)
            order.append("abs-done")
            return {"scan_complete": True, "items_total": 5, "waited_seconds": 1.0}

        async def fake_kavita_wait():
            order.append("kavita-start")
            return {"scan_complete": True, "waited_seconds": 2.0}

        with (
            patch.object(pipeline.audiobookshelf, "scan_library_and_wait", side_effect=fake_abs_wait),
            patch.object(pipeline.audiobookshelf, "remove_items_with_issues", new=AsyncMock()),
            patch.object(pipeline.audiobookshelf, "invalidate_cache"),
            patch.object(pipeline.kavita, "scan_library_and_wait", side_effect=fake_kavita_wait),
        ):
            kick = pipeline.kick()
            assert kick["started"] is True
            await pipeline._task

        # Kavita must only start after ABS fully finished.
        assert order == ["abs-start", "abs-done", "kavita-start"]
        status = pipeline.get_status()
        assert status["phase"] == "idle"
        assert status["abs"]["ok"] is True
        assert status["kavita"]["ok"] is True

    asyncio.run(_run())


def test_pipeline_coalesces_and_cooldowns_repeat_kicks():
    async def _run():
        _reset_pipeline()
        release = asyncio.Event()

        async def slow_abs():
            await release.wait()
            return {"scan_complete": True}

        with (
            patch.object(pipeline.audiobookshelf, "scan_library_and_wait", side_effect=slow_abs),
            patch.object(pipeline.audiobookshelf, "remove_items_with_issues", new=AsyncMock()),
            patch.object(pipeline.audiobookshelf, "invalidate_cache"),
            patch.object(
                pipeline.kavita,
                "scan_library_and_wait",
                new=AsyncMock(return_value={"scan_complete": True}),
            ),
        ):
            first = pipeline.kick()
            assert first["started"] is True
            await asyncio.sleep(0)
            second = pipeline.kick()
            assert second["started"] is False
            assert second["already_running"] is True
            release.set()
            await pipeline._task
            # Within the cooldown window a new kick is skipped.
            third = pipeline.kick()
            assert third["started"] is False
            assert third["cooldown"] is True

    asyncio.run(_run())


def test_pipeline_kavita_still_runs_when_abs_fails():
    async def _run():
        _reset_pipeline()

        with (
            patch.object(
                pipeline.audiobookshelf,
                "scan_library_and_wait",
                new=AsyncMock(side_effect=RuntimeError("abs down")),
            ),
            patch.object(pipeline.audiobookshelf, "remove_items_with_issues", new=AsyncMock()),
            patch.object(pipeline.audiobookshelf, "invalidate_cache"),
            patch.object(
                pipeline.kavita,
                "scan_library_and_wait",
                new=AsyncMock(return_value={"scan_complete": True}),
            ) as kv,
        ):
            pipeline.kick()
            await pipeline._task

        kv.assert_awaited_once()
        status = pipeline.get_status()
        assert status["abs"]["ok"] is False
        assert status["kavita"]["ok"] is True
        assert status["phase"] == "idle"

    asyncio.run(_run())


def test_kavita_scan_wait_completes_when_last_scanned_advances():
    async def _run():
        calls = {"n": 0}

        async def fake_last_scanned(_lid=None):
            calls["n"] += 1
            return "2026-01-01T00:00:00" if calls["n"] < 3 else "2026-01-01T00:05:00"

        with (
            patch.object(kavita_svc, "get_library_last_scanned", side_effect=fake_last_scanned),
            patch.object(kavita_svc, "scan_library", new=AsyncMock()) as scan_mock,
            patch.object(kavita_svc, "invalidate_cache") as inval,
            patch.object(kavita_svc.asyncio, "sleep", new=AsyncMock()),
        ):
            out = await kavita_svc.scan_library_and_wait(timeout_seconds=60, poll_interval=0.01)

        scan_mock.assert_awaited_once()
        assert out["scan_ran"] is True
        assert out["scan_complete"] is True
        assert out["timed_out"] is False
        # Cache cleared only after the scan finished (never mid-scan).
        inval.assert_called_once()

    asyncio.run(_run())


def test_kavita_series_shrink_guard_keeps_last_good_snapshot():
    async def _run():
        kavita_svc._cache.clear()
        kavita_svc._last_good_series.clear()

        full = [{"id": i, "format": 3} for i in range(100)]
        partial = [{"id": i, "format": 3} for i in range(40)]
        responses = [full, partial]

        class FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, *a, **k):
                return FakeResp(responses.pop(0))

        with (
            patch.object(kavita_svc, "_conn", new=AsyncMock(return_value=("http://kv", "key", 1))),
            patch.object(kavita_svc.httpx, "AsyncClient", FakeClient),
        ):
            first = await kavita_svc.get_all_series(formats=[3, 4], force_refresh=True)
            assert len(first) == 100
            # Mid-scan partial snapshot must not replace the last good one.
            second = await kavita_svc.get_all_series(formats=[3, 4], force_refresh=True)
            assert len(second) == 100

    asyncio.run(_run())


def test_kavita_volumes_shrink_guard_only_while_pipeline_runs():
    async def _run():
        kavita_svc._cache.clear()
        kavita_svc._last_good_vols.clear()

        full = [{"id": 1}, {"id": 2}, {"id": 3}]
        partial = [{"id": 1}]
        responses = [list(full), list(partial), list(partial)]

        class FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, *a, **k):
                return FakeResp(responses.pop(0))

        with (
            patch.object(kavita_svc, "_conn", new=AsyncMock(return_value=("http://kv", "key", 1))),
            patch.object(kavita_svc.httpx, "AsyncClient", FakeClient),
        ):
            first = await kavita_svc.get_series_volumes(106)
            assert len(first) == 3

            # Mid-refresh a shrunken volume list is mid-scan noise: keep last good.
            kavita_svc._cache.clear()
            with patch.object(kavita_svc, "_refresh_pipeline_active", return_value=True):
                second = await kavita_svc.get_series_volumes(106)
            assert len(second) == 3

            # With no refresh running, a shrink is a real deletion: accept it.
            kavita_svc._cache.clear()
            with patch.object(kavita_svc, "_refresh_pipeline_active", return_value=False):
                third = await kavita_svc.get_series_volumes(106)
            assert len(third) == 1

    asyncio.run(_run())


def test_collection_cache_invalidate_keeps_stale_payload():
    from app.services import library_collection_cache as coll

    coll._CACHE.clear()
    payload = {"items": ["a", "b", "c"], "totalItems": 3}
    coll.set("kavita_coll:1", payload)

    coll.invalidate()

    # Fresh reads miss (forces a rebuild when safe) ...
    assert coll.get("kavita_coll:1") is None
    # ... but the stale payload survives to serve while a scan is running.
    assert coll.get_stale("kavita_coll:1") == payload
