"""Unit tests for Hardcover library-item matching / enrichment."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services import hardcover
from app.routers import library as library_router


@pytest.fixture(autouse=True)
def _clear_hc_cache():
    hardcover._cache.clear()
    yield
    hardcover._cache.clear()


def test_match_library_book_prefers_compatible_hit():
    async def _run():
        hit = {
            "title": "The Gates of Sleep",
            "authors": ["Mercedes Lackey"],
            "categories": ["Fantasy", "Audiobook", "Fiction"],
            "seriesName": "Elemental Masters",
            "seriesBookNumber": "3",
        }
        with (
            patch.object(hardcover, "get_api_key", new=AsyncMock(return_value="k")),
            patch.object(hardcover, "search_books", new=AsyncMock(return_value=[hit])),
            patch.object(hardcover, "get_series_for_book", new=AsyncMock()) as gs,
        ):
            out = await hardcover.match_library_book(
                title="The Gates of Sleep",
                author="Mercedes Lackey",
            )
        assert out["author"] == "Mercedes Lackey"
        assert out["seriesName"] == "Elemental Masters"
        assert out["sequence"] == "3"
        assert out["matchedTitle"] == "The Gates of Sleep"
        assert "Fantasy" in out["genres"]
        gs.assert_not_called()

    asyncio.run(_run())


def test_match_library_book_falls_back_to_series_lookup():
    async def _run():
        hit = {
            "title": "Arrow's Fall",
            "authors": ["Mercedes Lackey"],
            "categories": ["Fantasy"],
            "seriesName": "",
            "seriesBookNumber": "",
        }
        series = {
            "seriesName": "Heralds of Valdemar",
            "books": [
                {"title": "Arrows of the Queen", "sequence": "1"},
                {"title": "Arrow's Fall", "sequence": "3"},
            ],
            "currentBookIndex": 1,
        }
        with (
            patch.object(hardcover, "get_api_key", new=AsyncMock(return_value="k")),
            patch.object(hardcover, "search_books", new=AsyncMock(return_value=[hit])),
            patch.object(hardcover, "get_series_for_book", new=AsyncMock(return_value=series)),
        ):
            out = await hardcover.match_library_book(
                title="Arrow's Fall",
                author="Mercedes Lackey",
                series_hint="Heralds of Valdemar",
            )
        assert out["seriesName"] == "Heralds of Valdemar"
        assert out["sequence"] == "3"

    asyncio.run(_run())


def test_match_library_book_caches_empty():
    async def _run():
        with (
            patch.object(hardcover, "get_api_key", new=AsyncMock(return_value="k")),
            patch.object(hardcover, "search_books", new=AsyncMock(return_value=[])) as sb,
        ):
            a = await hardcover.match_library_book(title="Unknown Book XYZ", author="Nobody")
            b = await hardcover.match_library_book(title="Unknown Book XYZ", author="Nobody")
        assert a["seriesName"] == ""
        assert b["seriesName"] == ""
        assert sb.await_count == 1

    asyncio.run(_run())


def test_enrich_items_maps_genres_via_taxonomy():
    async def _run():
        items = [
            {
                "itemId": "a1",
                "title": "The Gates of Sleep",
                "author": "M. Lackey",
                "genres": ["Audiobook"],
                "series": [],
            }
        ]
        match = {
            "author": "Mercedes Lackey",
            "genres": ["Fantasy", "Audiobook", "Fiction"],
            "seriesName": "Elemental Masters",
            "sequence": "3",
            "matchedTitle": "The Gates of Sleep",
        }
        with patch.object(
            hardcover, "match_library_book", new=AsyncMock(return_value=match)
        ):
            out = await library_router._enrich_items_via_hardcover(items)
        assert len(out) == 1
        assert out[0]["author"] == "Mercedes Lackey"
        assert out[0]["seriesName"] == "Elemental Masters"
        assert out[0]["sequence"] == "3"
        assert "Fantasy" in out[0]["genres"]
        assert "Audiobook" not in out[0]["genres"]
        assert "Fiction" not in out[0]["genres"]

    asyncio.run(_run())


def test_group_prefers_enriched_series_name():
    async def _run():
        items = [
            {
                "itemId": "1",
                "title": "Book One",
                "author": "Author",
                "seriesName": "My Series",
                "sequence": "1",
                "coverUrl": "",
                "duration": 0,
            },
            {
                "itemId": "2",
                "title": "Book Two",
                "author": "Author",
                "seriesName": "My Series",
                "sequence": "2",
                "coverUrl": "",
                "duration": 0,
            },
        ]
        with patch.object(hardcover, "get_series_for_book", new=AsyncMock()) as gs:
            groups = await library_router._group_items_by_hardcover_series(items)
        assert len(groups) == 1
        assert groups[0]["name"] == "My Series"
        assert groups[0]["bookCount"] == 2
        gs.assert_not_called()

    asyncio.run(_run())
