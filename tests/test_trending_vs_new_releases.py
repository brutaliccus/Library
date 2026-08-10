"""Trending must not collapse onto the same recent-matched pool as New Releases."""
from __future__ import annotations

import asyncio

from app.routers import books


def test_trending_pads_with_score_not_recent(monkeypatch):
    calls: list[str] = []

    async def fake_nyt(limit: int = 20):
        return []

    async def fake_list_matched(*, page, page_size, order_by="score", need_total=False):
        calls.append(order_by)
        if order_by == "recent":
            return [f"OL:recent:{i}" for i in range(page_size)], page_size
        return [f"OL:score:{i}" for i in range(page_size)], page_size

    async def fake_cards(volume_ids):
        return [
            {"id": vid, "volumeId": vid, "title": vid, "authors": []}
            for vid in volume_ids
        ]

    monkeypatch.setattr(books, "_nyt_trending_available_cards", fake_nyt)
    monkeypatch.setattr(books.indexer_cache, "list_matched_volume_ids", fake_list_matched)
    monkeypatch.setattr(books, "_fetch_volume_cards", fake_cards)

    payload = asyncio.run(books.build_trending_payload())
    assert payload["books"]
    assert all(str(b["id"]).startswith("OL:score:") for b in payload["books"])
    assert "recent" in calls
    assert calls.count("score") >= 1
    assert payload["source"] in ("popular", "nyt+popular")


def test_trending_excludes_recent_ids_when_padding_nyt(monkeypatch):
    async def fake_nyt(limit: int = 20):
        return [{"id": "OL:nyt:1", "volumeId": "OL:nyt:1", "title": "NYT Hit"}]

    async def fake_list_matched(*, page, page_size, order_by="score", need_total=False):
        if order_by == "recent":
            return ["OL:score:0", "OL:recent:9"], 2
        return [f"OL:score:{i}" for i in range(30)], 30

    async def fake_cards(volume_ids):
        return [
            {"id": vid, "volumeId": vid, "title": vid, "authors": []}
            for vid in volume_ids
        ]

    monkeypatch.setattr(books, "_nyt_trending_available_cards", fake_nyt)
    monkeypatch.setattr(books.indexer_cache, "list_matched_volume_ids", fake_list_matched)
    monkeypatch.setattr(books, "_fetch_volume_cards", fake_cards)

    payload = asyncio.run(books.build_trending_payload())
    ids = {b["id"] for b in payload["books"]}
    assert "OL:nyt:1" in ids
    assert "OL:score:0" not in ids
    assert any(i.startswith("OL:score:") for i in ids)