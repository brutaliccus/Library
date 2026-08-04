"""ABS shelf dedupe: ASIN twins + chapter-folder fragments."""

from __future__ import annotations

from app.routers.library import _dedupe_abs_shelf_items


def test_dedupe_abs_by_asin_keeps_richer_item():
    items = [
        {
            "itemId": "a",
            "title": "Assassin's Quest",
            "author": "Robin Hobb",
            "asin": "B003XWVC0E",
            "numTracks": 1,
            "duration": 100,
            "relPath": "Robin Hobb/Assassin's Quest",
        },
        {
            "itemId": "b",
            "title": "Assassin's Quest",
            "author": "Robin Hobb",
            "asin": "B003XWVC0E",
            "numTracks": 12,
            "duration": 135996,
            "relPath": "Robin Hobb/Realms/Assassin's Quest",
        },
    ]
    out = _dedupe_abs_shelf_items(items)
    assert len(out) == 1
    assert out[0]["itemId"] == "b"


def test_dedupe_abs_collapses_chapter_folder_fragments():
    items = []
    for i, folder in enumerate(
        [
            "Prologue-A Strained Conversation",
            "Ch 01 - Little Games 01",
            "Ch 02 - Requin",
            "Reminiscence 01 - The Cape",
            "Epilogue - Red Seas Under Red Skies 01",
        ]
    ):
        items.append(
            {
                "itemId": f"rs-{i}",
                "title": "Red Seas Under Red Skies",
                "author": "Scott Lynch",
                "asin": "",
                "numTracks": 1,
                "duration": 1000 + i * 100,
                "relPath": f"Scott Lynch/{folder}",
            }
        )
    out = _dedupe_abs_shelf_items(items)
    assert len(out) == 1
    assert out[0]["title"] == "Red Seas Under Red Skies"


def test_dedupe_abs_keeps_unrelated_distinct_titles():
    items = [
        {
            "itemId": "1",
            "title": "Book A",
            "author": "Author",
            "asin": "B000000001",
            "numTracks": 10,
            "duration": 50000,
            "relPath": "Author/Book A",
        },
        {
            "itemId": "2",
            "title": "Book B",
            "author": "Author",
            "asin": "B000000002",
            "numTracks": 10,
            "duration": 50000,
            "relPath": "Author/Book B",
        },
    ]
    out = _dedupe_abs_shelf_items(items)
    assert {i["itemId"] for i in out} == {"1", "2"}