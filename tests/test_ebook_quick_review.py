"""Tests for ebook Hardcover metadata matcher (load/search/apply helpers)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ebook_pipeline import EbookMeta
from app.services.ebook_quick_review import (
    EbookQuickReviewError,
    list_ebook_staging_targets,
    load_applied_ebook_meta,
    selected_result_to_ebook_meta,
    write_applied_ebook_meta,
)


@pytest.fixture(autouse=True)
def _ebook_dirs(tmp_path, monkeypatch):
    ebook = tmp_path / "ebooks"
    audio = tmp_path / "audiobooks"
    ebook.mkdir()
    audio.mkdir()
    monkeypatch.setattr("app.services.ebook_pipeline.settings.ebook_dir", str(ebook))
    monkeypatch.setattr("app.services.forge_pipeline.settings.ebook_dir", str(ebook))
    monkeypatch.setattr("app.services.forge_pipeline.settings.audiobook_dir", str(audio))
    return ebook, audio


def test_write_and_load_applied_ebook_meta(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = EbookMeta(
        title="Timeline",
        author="Michael Crichton",
        series="Standalone",
        series_index=None,
        isbn13="9780345468260",
        score=0.97,
        source="hardcover",
        cover_url="https://example.com/cover.jpg",
        reason="Manual Hardcover match",
    )
    path = write_applied_ebook_meta(staging, meta)
    assert path.name == "ebook_applied.json"
    loaded = load_applied_ebook_meta(staging)
    assert loaded is not None
    assert loaded.title == "Timeline"
    assert loaded.author == "Michael Crichton"
    assert loaded.isbn13 == "9780345468260"
    assert loaded.cover_url == "https://example.com/cover.jpg"
    assert loaded.source == "hardcover"


def test_list_ebook_staging_targets_finds_epub(tmp_path):
    staging = tmp_path / "req_1_book"
    staging.mkdir()
    (staging / "book.epub").write_bytes(b"epub")
    targets = list_ebook_staging_targets(staging)
    assert len(targets) == 1
    assert targets[0]["file_count"] == 1


def test_selected_result_to_ebook_meta_requires_title():
    with pytest.raises(EbookQuickReviewError):
        selected_result_to_ebook_meta({"authors": ["Someone"]})


def test_selected_result_to_ebook_meta_maps_hardcover_fields():
    meta = selected_result_to_ebook_meta(
        {
            "title": "Ash and Quill",
            "authors": ["Rachel Caine"],
            "series": "Great Library",
            "sequence": "3",
            "isbn13": "9780451472410",
            "cover_url": "https://example.com/a.jpg",
            "score": 0.88,
        }
    )
    assert meta.title == "Ash and Quill"
    assert meta.author == "Rachel Caine"
    assert meta.series == "Great Library"
    assert meta.series_index == "3"
    assert meta.cover_url == "https://example.com/a.jpg"


def test_search_ebook_quick_review_calls_hardcover(tmp_path, monkeypatch):
    import asyncio

    from app.services import ebook_quick_review as eqr

    ebook = tmp_path / "ebooks"
    staging = ebook / "unorganized" / "req_9_Timeline"
    staging.mkdir(parents=True)
    (staging / "Timeline.epub").write_bytes(b"epub")
    monkeypatch.setattr("app.services.ebook_pipeline.settings.ebook_dir", str(ebook))
    monkeypatch.setattr("app.services.forge_pipeline.settings.ebook_dir", str(ebook))

    req = MagicMock()
    req.id = 9
    req.media_type = "ebook"
    req.staging_path = str(staging.as_posix())
    req.title = "Timeline"
    req.author = "Michael Crichton"

    hits = [
        {
            "id": "HC:1",
            "title": "Timeline",
            "authors": ["Michael Crichton"],
            "coverUrl": "https://example.com/c.jpg",
            "seriesName": "",
            "seriesBookNumber": "",
            "isbn13": "9780345468260",
            "isbn10": "",
            "publishedDate": "1999",
            "description": "A novel",
            "publisher": "",
            "language": "en",
            "hardcoverId": 1,
            "hardcoverSlug": "timeline",
            "infoLink": "https://hardcover.app/books/timeline",
            "previewLink": "",
        }
    ]

    async def _run():
        with (
            patch.object(eqr, "resolve_staging_dir", return_value=staging),
            patch("app.services.hardcover.get_api_key", new=AsyncMock(return_value="Bearer x")),
            patch("app.services.hardcover.search_books", new=AsyncMock(return_value=hits)),
        ):
            return await eqr.search_ebook_quick_review(
                req,
                query="Timeline Michael Crichton",
                title="Timeline",
                author="Michael Crichton",
            )

    out = asyncio.run(_run())
    assert out["provider"] == "hardcover"
    assert len(out["results"]) == 1
    assert out["results"][0]["title"] == "Timeline"
    assert out["results"][0]["cover_url"] == "https://example.com/c.jpg"
