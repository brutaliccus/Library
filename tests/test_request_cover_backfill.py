"""Tests for DownloadRequest cover persistence / list backfill."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_lookup_cover_url_prefers_volume_then_search():
    from app.services import google_books

    async def _run():
        with (
            patch.object(
                google_books,
                "get_catalog_volume",
                new=AsyncMock(return_value={"coverUrl": "https://cdn.example/cover.jpg"}),
            ),
            patch.object(google_books, "search_volumes", new=AsyncMock()) as search,
        ):
            out = await google_books.lookup_cover_url("OL:123", "Dune", "Herbert")
        assert out == "https://cdn.example/cover.jpg"
        search.assert_not_called()

    asyncio.run(_run())


def test_lookup_cover_url_falls_back_to_title_search():
    from app.services import google_books

    async def _run():
        with (
            patch.object(google_books, "get_catalog_volume", new=AsyncMock(return_value=None)),
            patch.object(
                google_books,
                "search_volumes",
                new=AsyncMock(
                    return_value={
                        "books": [
                            {"title": "Dune", "coverUrl": "https://cdn.example/dune.jpg"},
                        ]
                    }
                ),
            ),
        ):
            out = await google_books.lookup_cover_url(None, "Dune", "Herbert")
        assert out == "https://cdn.example/dune.jpg"

    asyncio.run(_run())


def test_backfill_missing_request_covers_persists():
    from app.routers import requests as requests_router

    async def _run():
        req_missing = MagicMock()
        req_missing.cover_url = None
        req_missing.google_volume_id = "OL:1"
        req_missing.title = "Dune"
        req_missing.author = "Herbert"

        req_ok = MagicMock()
        req_ok.cover_url = "https://cdn.example/already.jpg"
        req_ok.google_volume_id = "OL:2"
        req_ok.title = "Other"
        req_ok.author = "A"

        with patch.object(
            requests_router.google_books,
            "lookup_cover_url",
            new=AsyncMock(return_value="https://cdn.example/filled.jpg"),
        ) as lookup:
            dirty = await requests_router._backfill_request_covers([req_missing, req_ok])

        assert dirty is True
        assert req_missing.cover_url == "https://cdn.example/filled.jpg"
        assert req_ok.cover_url == "https://cdn.example/already.jpg"
        lookup.assert_awaited_once_with("OL:1", "Dune", "Herbert")

    asyncio.run(_run())
