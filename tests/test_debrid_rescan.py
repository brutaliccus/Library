"""Tests for full debrid rescan helpers."""

import asyncio

from app.services import indexer_scraper


def test_get_debrid_rescan_progress_defaults_empty():
    indexer_scraper._debrid_rescan_progress.clear()
    assert indexer_scraper.get_debrid_rescan_progress() == {}


async def _noop_loop(_batch_size: int) -> None:
    return None


def test_start_full_debrid_rescan_rejects_when_already_running(monkeypatch):
    indexer_scraper._debrid_rescan_progress.clear()

    async def _running_state():
        return {"running": True, "queued": 10, "pending": 5}

    async def _fail_queue(*_args, **_kwargs):
        raise AssertionError("queue_all_debrid_recheck should not run when rescan is active")

    monkeypatch.setattr(indexer_scraper, "_load_debrid_rescan_state", _running_state)
    monkeypatch.setattr(indexer_scraper.indexer_cache, "queue_all_debrid_recheck", _fail_queue)

    result = asyncio.run(indexer_scraper.start_full_debrid_rescan())
    assert result["ok"] is False
    assert "already running" in result["error"]
    indexer_scraper._debrid_rescan_progress.clear()
