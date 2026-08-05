"""Ebook cover URL builder + per-volume detail identity."""

from __future__ import annotations

from app.routers import library as library_router


def test_ebook_cover_url_includes_volume_and_chapter():
    assert library_router._ebook_cover_url(None) == ""
    assert library_router._ebook_cover_url(12) == "/api/library/reader/cover/ebook?seriesId=12"
    assert (
        library_router._ebook_cover_url(12, volume_id=3, chapter_id=9)
        == "/api/library/reader/cover/ebook?seriesId=12" + chr(38) + "volumeId=3" + chr(38) + "chapterId=9"
    )


def test_collection_items_stamp_per_volume_covers():
    series = {"id": 42, "name": "Dungeon Crawler Carl", "created": 0}
    volumes = [
        {
            "id": 1,
            "number": 1,
            "chapters": [
                {
                    "id": 101,
                    "files": [{"filePath": "/books/DCC/Dungeon Crawler Carl.epub"}],
                }
            ],
        },
        {
            "id": 2,
            "number": 2,
            "chapters": [
                {
                    "id": 102,
                    "files": [{"filePath": "/books/DCC/Carls Doomsday Scenario.epub"}],
                }
            ],
        },
    ]
    items = library_router._kavita_collection_items_from_series(
        series, volumes, meta={"writers": [{"name": "Matt Dinniman"}], "genres": []},
        hidden_titles=set(),
    )
    assert len(items) == 2
    amp = chr(38)
    assert items[0]["coverUrl"] == f"/api/library/reader/cover/ebook?seriesId=42{amp}volumeId=1{amp}chapterId=101"
    assert items[1]["coverUrl"] == f"/api/library/reader/cover/ebook?seriesId=42{amp}volumeId=2{amp}chapterId=102"
    assert items[0]["chapterId"] != items[1]["chapterId"]