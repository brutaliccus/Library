"""Tests for in-library metadata review helpers."""

from __future__ import annotations

from app.services.library_metadata_review import _abs_payload_from_selected


def test_abs_payload_from_selected_maps_core_fields():
    selected = {
        "title": "Timeline",
        "authors": ["Michael Crichton"],
        "narrators": ["Actor One"],
        "series": "Standalone",
        "sequence": "1",
        "asin": "B00TESTASIN",
        "year": "1999",
        "cover_url": "https://example.com/c.jpg",
        "chosen_metadata": {
            "title": "Timeline",
            "author": "Michael Crichton",
            "narrator": "Actor One",
            "series": "Standalone",
            "sequence": "1",
            "asin": "B00TESTASIN",
            "year": "1999",
            "cover_url": "https://example.com/c.jpg",
            "summary": "A blurb",
        },
    }
    payload, cover = _abs_payload_from_selected(selected, edit_mode="full")
    assert payload["title"] == "Timeline"
    assert payload["authors"] == [{"name": "Michael Crichton"}]
    assert payload["narrators"] == ["Actor One"]
    assert payload["asin"] == "B00TESTASIN"
    assert payload["publishedYear"] == "1999"
    assert payload["description"] == "A blurb"
    assert payload["series"] == [{"name": "Standalone", "sequence": "1"}]
    assert payload["seriesName"] == "Standalone #1"
    assert cover == "https://example.com/c.jpg"


def test_abs_payload_skips_series_when_equals_title():
    selected = {
        "title": "Solo Book",
        "chosen_metadata": {
            "title": "Solo Book",
            "series": "Solo Book",
            "author": "Someone",
        },
    }
    payload, _cover = _abs_payload_from_selected(selected, edit_mode="full")
    assert "series" not in payload
