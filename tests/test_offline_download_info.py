"""Offline ABS package includes chapters, cover URL, and catalog metadata."""

from __future__ import annotations

from app.services.audiobookshelf import offline_download_info_from_item


def test_offline_download_info_includes_chapters_cover_and_metadata():
    item = {
        "id": "item-abc",
        "title": "Fallback Title",
        "media": {
            "duration": 120.0,
            "chapters": [
                {"id": 0, "title": "Opening", "start": 0, "end": 30},
                {"id": 1, "title": "Chapter 1", "start": 30, "end": 120},
            ],
            "tracks": [
                {
                    "ino": "file-1",
                    "index": 0,
                    "duration": 120.0,
                    "startOffset": 0,
                    "title": "Track 1",
                    "mimeType": "audio/mp4",
                }
            ],
            "metadata": {
                "title": "The Book",
                "subtitle": "A Tale",
                "authorName": "Jane Author",
                "narratorName": "Sam Voice",
                "seriesName": "Saga #1",
                "description": "A complete story.",
                "asin": "B0TESTASIN1",
                "genres": ["Fantasy"],
                "publishedYear": "2024",
            },
        },
    }
    info = offline_download_info_from_item(item)
    assert info is not None
    assert info["title"] == "The Book"
    assert info["author"] == "Jane Author"
    assert info["narrator"] == "Sam Voice"
    assert info["seriesName"] == "Saga"
    assert info["sequence"] == "1"
    assert info["asin"] == "B0TESTASIN1"
    assert info["description"] == "A complete story."
    assert info["coverUrl"] == "/api/stream/abs/proxy/cover/item-abc"
    assert len(info["tracks"]) == 1
    assert info["tracks"][0]["contentUrl"].endswith("/item-abc/file-1")
    assert len(info["chapters"]) == 2
    assert info["chapters"][0]["title"] == "Opening"
    assert info["chapters"][1]["start"] == 30


def test_offline_download_info_requires_audio_tracks():
    assert offline_download_info_from_item({"id": "x", "media": {"metadata": {}}}) is None