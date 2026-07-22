"""Unit tests for library-item genre enrichment and local series grouping."""

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


def test_enrich_items_maps_genres_only_keeps_local_author_series():
    async def _run():
        items = [
            {
                "itemId": "a1",
                "title": "The Gates of Sleep",
                "author": "M. Lackey",
                "genres": ["Audiobook"],
                "seriesName": "Elemental Masters",
                "sequence": "3",
                "series": [{"name": "Elemental Masters", "sequence": "3"}],
            }
        ]
        match = {
            "author": "Mercedes Lackey",
            "genres": ["Fantasy", "Audiobook", "Fiction"],
            "seriesName": "HC Wrong Series",
            "sequence": "99",
            "matchedTitle": "The Gates of Sleep",
        }
        with patch.object(
            hardcover, "match_library_book", new=AsyncMock(return_value=match)
        ):
            out = await library_router._enrich_items_via_hardcover(items)
        assert len(out) == 1
        # Genres enriched + taxonomy-mapped
        assert "Fantasy" in out[0]["genres"]
        assert "Audiobook" not in out[0]["genres"]
        assert "Fiction" not in out[0]["genres"]
        # Author / series stay local — not overwritten by Hardcover
        assert out[0]["author"] == "M. Lackey"
        assert out[0]["seriesName"] == "Elemental Masters"
        assert out[0]["sequence"] == "3"

    asyncio.run(_run())


def test_enrich_items_fail_open_on_match_error():
    async def _run():
        items = [
            {"itemId": "1", "title": "Keep Me", "author": "A", "genres": ["Fantasy"]},
            {"itemId": "2", "title": "Also Keep", "author": "B", "genres": []},
        ]

        async def boom(**kwargs):
            raise RuntimeError("Hardcover down")

        with patch.object(hardcover, "match_library_book", new=boom):
            out = await library_router._enrich_items_via_hardcover(items)
        assert len(out) == 2
        assert out[0]["title"] == "Keep Me"
        assert out[1]["title"] == "Also Keep"

    asyncio.run(_run())


def test_enrich_items_fail_open_on_budget_timeout():
    async def _run():
        items = [
            {"itemId": "1", "title": "Slow Book", "author": "A", "genres": ["Fantasy"]},
        ]

        async def slow(**kwargs):
            await asyncio.sleep(2)
            return {"author": "X", "genres": [], "seriesName": "", "sequence": "", "matchedTitle": ""}

        with patch.object(hardcover, "match_library_book", new=slow):
            out = await library_router._enrich_items_via_hardcover(
                items, budget_seconds=0.05
            )
        assert len(out) == 1
        assert out[0]["title"] == "Slow Book"
        assert out[0]["author"] == "A"  # unenriched original

    asyncio.run(_run())


def test_normalize_item_genres_tolerates_weird_shapes():
    # Must not raise on None / int / odd dict shapes.
    out = library_router._normalize_item_genres(
        ["Fantasy", None, 12, {"name": "Mystery"}, {"nope": True}]
    )
    assert "Fantasy" in out
    assert any("Mystery" in g for g in out)


def test_group_by_local_series_no_hardcover_calls():
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
        {
            "itemId": "3",
            "title": "Standalone",
            "author": "Author",
            "seriesName": "",
            "series": [],
            "coverUrl": "",
            "duration": 0,
        },
    ]
    with patch.object(hardcover, "get_series_for_book", new=AsyncMock()) as gs:
        with patch.object(hardcover, "match_library_book", new=AsyncMock()) as mb:
            groups = library_router._group_items_by_local_series(items)
    assert len(groups) == 1
    assert groups[0]["name"] == "My Series"
    assert groups[0]["bookCount"] == 2
    gs.assert_not_called()
    mb.assert_not_called()


def test_group_by_local_series_from_series_array_and_title():
    items = [
        {
            "itemId": "1",
            "title": "Phoenix and Ashes (Elemental Masters, Book 3)",
            "author": "Mercedes Lackey",
            "series": [],
            "coverUrl": "",
            "duration": 10,
        },
        {
            "itemId": "2",
            "title": "The Gates of Sleep",
            "author": "Mercedes Lackey",
            "series": [{"name": "Elemental Masters", "sequence": "1"}],
            "coverUrl": "c2",
            "duration": 20,
        },
    ]
    groups = library_router._group_items_by_local_series(items)
    assert len(groups) == 1
    assert groups[0]["name"] == "Elemental Masters"
    assert groups[0]["bookCount"] == 2


def test_local_series_from_item_skips_junk():
    name, seq = library_router._local_series_from_item(
        {"title": "Some Book", "seriesName": "B0ABCDEF12", "series": []}
    )
    assert name == ""
    assert seq == ""
