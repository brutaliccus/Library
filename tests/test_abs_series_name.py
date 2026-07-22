"""ABS Folder Forge stores series in metadata.seriesName ('Series #N'), not series[]."""

from __future__ import annotations

from app.services.audiobookshelf import _normalize_abs_item
from app.routers import library as library_router
from app.utils.book_series import parse_abs_series_label


def test_parse_abs_series_label_hash_sequence():
    assert parse_abs_series_label("Dungeon Crawler Carl #1") == ("Dungeon Crawler Carl", "1")
    assert parse_abs_series_label("Dungeon Crawler Carl #6") == ("Dungeon Crawler Carl", "6")
    assert parse_abs_series_label("Throne of Glass #0.1") == ("Throne of Glass", "0.1")


def test_parse_abs_series_label_plain_and_book():
    assert parse_abs_series_label("Practical Magic") == ("Practical Magic", "")
    assert parse_abs_series_label("Elemental Masters, Book 3") == ("Elemental Masters", "3")
    assert parse_abs_series_label("Elemental Masters Book 3") == ("Elemental Masters", "3")


def test_parse_abs_series_label_junk():
    assert parse_abs_series_label("B0ABCDEF12") == ("", "")
    assert parse_abs_series_label("Audiobook") == ("", "")
    assert parse_abs_series_label("") == ("", "")
    assert parse_abs_series_label(None) == ("", "")


def test_normalize_abs_item_flattens_series_name_string():
    """Live ABS after Folder Forge: series=null, seriesName='Dungeon Crawler Carl #1'."""
    raw = {
        "id": "li_dcc1",
        "addedAt": 1,
        "media": {
            "duration": 100,
            "numTracks": 1,
            "metadata": {
                "title": "Dungeon Crawler Carl",
                "authorName": "Matt Dinniman",
                "genres": ["Fantasy"],
                "series": None,
                "seriesName": "Dungeon Crawler Carl #1",
            },
        },
    }
    out = _normalize_abs_item(raw)
    assert out["seriesName"] == "Dungeon Crawler Carl"
    assert out["sequence"] == "1"
    assert out["series"] == [{"id": "", "name": "Dungeon Crawler Carl", "sequence": "1"}]


def test_normalize_abs_item_prefers_series_array():
    raw = {
        "id": "li_x",
        "addedAt": 1,
        "media": {
            "duration": 10,
            "metadata": {
                "title": "Book",
                "authorName": "A",
                "series": [{"id": "s1", "name": "Real Series", "sequence": "2"}],
                "seriesName": "Ignored #9",
            },
        },
    }
    out = _normalize_abs_item(raw)
    assert out["seriesName"] == "Real Series"
    assert out["sequence"] == "2"
    assert out["series"][0]["name"] == "Real Series"


def test_apply_local_series_fields_from_abs_series_name():
    item = {
        "itemId": "1",
        "title": "Carl's Doomsday Scenario",
        "series": [],
        "seriesName": "Dungeon Crawler Carl #2",
    }
    out = library_router._apply_local_series_fields(item)
    assert out["seriesName"] == "Dungeon Crawler Carl"
    assert out["sequence"] == "2"


def test_local_series_groups_hash_series_names():
    items = [
        {
            "itemId": "1",
            "title": "Dungeon Crawler Carl",
            "seriesName": "Dungeon Crawler Carl #1",
            "series": [],
            "coverUrl": "",
            "duration": 10,
        },
        {
            "itemId": "2",
            "title": "Carl's Doomsday Scenario",
            "seriesName": "Dungeon Crawler Carl #2",
            "series": [],
            "coverUrl": "c2",
            "duration": 20,
        },
    ]
    groups = library_router._group_items_by_local_series(items)
    assert len(groups) == 1
    assert groups[0]["name"] == "Dungeon Crawler Carl"
    assert groups[0]["bookCount"] == 2
