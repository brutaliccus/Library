"""Tests for ebook Hardcover + Google Books + Open Library metadata matcher helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ebook_pipeline import EbookMeta
from app.services.ebook_quick_review import (
    EbookQuickReviewError,
    apply_ebook_override_fields,
    list_ebook_staging_targets,
    load_applied_ebook_meta,
    load_applied_ebook_override,
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
            "source": "hardcover",
        }
    )
    assert meta.title == "Ash and Quill"
    assert meta.author == "Rachel Caine"
    assert meta.series == "Great Library"
    assert meta.series_index == "3"
    assert meta.cover_url == "https://example.com/a.jpg"
    assert meta.source == "hardcover"
    assert "Hardcover" in meta.reason


def test_selected_result_to_ebook_meta_maps_open_library_fields():
    meta = selected_result_to_ebook_meta(
        {
            "id": "OL:/works/OL45804W",
            "title": "Timeline",
            "authors": ["Michael Crichton"],
            "isbn13": "9780345468260",
            "cover_url": "https://covers.openlibrary.org/b/id/1-M.jpg",
            "source": "open_library",
            "score": 0.9,
        }
    )
    assert meta.source == "open_library"
    assert meta.isbn13 == "9780345468260"
    assert "Open Library" in meta.reason



def test_selected_result_to_ebook_meta_maps_google_books_fields():
    meta = selected_result_to_ebook_meta(
        {
            "id": "zyTCAlFPjgYC",
            "title": "Timeline",
            "authors": ["Michael Crichton"],
            "isbn13": "9780345468260",
            "cover_url": "https://books.google.com/cover.jpg",
            "source": "google_books",
            "score": 0.91,
        }
    )
    assert meta.source == "google_books"
    assert meta.isbn13 == "9780345468260"
    assert "Google Books" in meta.reason


def test_search_ebook_quick_review_merges_hardcover_gb_and_ol(tmp_path, monkeypatch):
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

    hc_hits = [
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
    gb_books = [
        {
            "id": "gbTimeline1",
            "title": "Timeline",
            "authors": ["Michael Crichton"],
            "coverUrl": "https://books.google.com/cover.jpg",
            "isbn13": "9780345468260",
            "isbn10": "",
            "publishedDate": "1999",
            "description": "",
            "publisher": "Ballantine",
            "language": "en",
            "infoLink": "https://books.google.com/books?id=gbTimeline1",
        }
    ]
    ol_books = [
        {
            "id": "OL:/works/OL45804W",
            "volumeId": "OL:/works/OL45804W",
            "title": "Timeline",
            "authors": ["Michael Crichton"],
            "coverUrl": "https://covers.openlibrary.org/b/id/99-M.jpg",
            "isbn13": "9780345468260",
            "isbn10": "",
            "publishedDate": "1999",
            "description": "",
            "publisher": "Ballantine",
            "language": "en",
            "infoLink": "https://openlibrary.org/works/OL45804W",
            "previewLink": "https://openlibrary.org/works/OL45804W",
        },
        {
            "id": "OL:/works/OL999W",
            "volumeId": "OL:/works/OL999W",
            "title": "Timeline (unrelated)",
            "authors": ["Someone Else"],
            "coverUrl": "",
            "isbn13": "",
            "isbn10": "",
            "publishedDate": "2001",
            "description": "",
            "publisher": "",
            "language": "en",
            "infoLink": "https://openlibrary.org/works/OL999W",
            "previewLink": "",
        },
    ]

    async def _run():
        with (
            patch.object(eqr, "resolve_staging_dir", return_value=staging),
            patch("app.services.hardcover.get_api_key", new=AsyncMock(return_value="Bearer x")),
            patch("app.services.hardcover.search_books", new=AsyncMock(return_value=hc_hits)),
            patch(
                "app.services.google_books.search_google_books",
                new=AsyncMock(return_value={"books": gb_books, "totalItems": 1}),
            ),
            patch(
                "app.services.google_books.search_open_library",
                new=AsyncMock(return_value=ol_books),
            ),
            patch(
                "app.config.get_settings",
                return_value=type("S", (), {"google_books_api_key": "test-key"})(),
            ),
        ):
            return await eqr.search_ebook_quick_review(
                req,
                query="Timeline Michael Crichton",
                title="Timeline",
                author="Michael Crichton",
            )

    out = asyncio.run(_run())
    assert out["provider"] == "hardcover+google_books+open_library"
    assert "hardcover" in out["providers"]
    assert "google_books" in out["providers"]
    assert "open_library" in out["providers"]
    # ISBN-identical HC + GB + OL rows collapse; unrelated OL remains.
    assert len(out["results"]) == 2
    top = out["results"][0]
    assert top["title"] == "Timeline"
    assert top["source"] == "hardcover"
    assert top["cover_url"]  # kept / filled
    assert any(r["source"] == "open_library" for r in out["results"])


def test_search_ebook_quick_review_ol_only_without_hardcover_key(tmp_path, monkeypatch):
    import asyncio

    from app.services import ebook_quick_review as eqr

    ebook = tmp_path / "ebooks"
    staging = ebook / "unorganized" / "req_10_Book"
    staging.mkdir(parents=True)
    (staging / "Book.epub").write_bytes(b"epub")
    monkeypatch.setattr("app.services.ebook_pipeline.settings.ebook_dir", str(ebook))
    monkeypatch.setattr("app.services.forge_pipeline.settings.ebook_dir", str(ebook))

    req = MagicMock()
    req.id = 10
    req.media_type = "ebook"
    req.staging_path = str(staging.as_posix())
    req.title = "Book"
    req.author = "Author"

    ol_books = [
        {
            "id": "OL:/works/OL1W",
            "title": "Book",
            "authors": ["Author"],
            "coverUrl": "https://covers.openlibrary.org/b/id/1-M.jpg",
            "isbn13": "",
            "isbn10": "",
            "publishedDate": "2020",
            "infoLink": "https://openlibrary.org/works/OL1W",
        }
    ]

    async def _run():
        with (
            patch.object(eqr, "resolve_staging_dir", return_value=staging),
            patch("app.services.hardcover.get_api_key", new=AsyncMock(return_value="")),
            patch(
                "app.services.google_books.search_open_library",
                new=AsyncMock(return_value=ol_books),
            ),
            patch(
                "app.config.get_settings",
                return_value=type("S", (), {"google_books_api_key": ""})(),
            ),
        ):
            return await eqr.search_ebook_quick_review(req, query="Book Author", title="Book")

    out = asyncio.run(_run())
    assert len(out["results"]) == 1
    assert out["results"][0]["source"] == "open_library"
    assert "open_library" in out["providers"]
    assert "google_books" not in out["providers"]


def test_search_ebook_quick_review_includes_google_books_candidates(tmp_path, monkeypatch):
    import asyncio

    from app.services import ebook_quick_review as eqr

    ebook = tmp_path / "ebooks"
    staging = ebook / "unorganized" / "req_11_Book"
    staging.mkdir(parents=True)
    (staging / "Book.epub").write_bytes(b"epub")
    monkeypatch.setattr("app.services.ebook_pipeline.settings.ebook_dir", str(ebook))
    monkeypatch.setattr("app.services.forge_pipeline.settings.ebook_dir", str(ebook))

    req = MagicMock()
    req.id = 11
    req.media_type = "ebook"
    req.staging_path = str(staging.as_posix())
    req.title = "Unique GB Title"
    req.author = "GB Author"

    gb_books = [
        {
            "id": "gbOnly1",
            "title": "Unique GB Title",
            "authors": ["GB Author"],
            "coverUrl": "https://books.google.com/c.jpg",
            "isbn13": "9781111111111",
            "publishedDate": "2021",
            "infoLink": "https://books.google.com/books?id=gbOnly1",
        }
    ]

    async def _run():
        with (
            patch.object(eqr, "resolve_staging_dir", return_value=staging),
            patch("app.services.hardcover.get_api_key", new=AsyncMock(return_value="")),
            patch(
                "app.services.google_books.search_google_books",
                new=AsyncMock(return_value={"books": gb_books, "totalItems": 1}),
            ),
            patch(
                "app.services.google_books.search_open_library",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.config.get_settings",
                return_value=type("S", (), {"google_books_api_key": "test-key"})(),
            ),
        ):
            return await eqr.search_ebook_quick_review(
                req, query="Unique GB Title GB Author", title="Unique GB Title"
            )

    out = asyncio.run(_run())
    assert len(out["results"]) == 1
    assert out["results"][0]["source"] == "google_books"
    assert "google_books" in out["providers"]


def test_library_override_sidecar_roundtrip(tmp_path):
    """Library ebook folder keeps applied metadata for shelf merge after Kavita refresh."""
    book_dir = tmp_path / "Author" / "Series" / "Book"
    book_dir.mkdir(parents=True)
    (book_dir / "Book.epub").write_bytes(b"epub")
    meta = EbookMeta(
        title="Pinned Title",
        author="Pinned Author",
        series="Pinned Series",
        series_index="2",
        score=0.99,
        source="hardcover",
    )
    write_applied_ebook_meta(
        book_dir,
        meta,
        summary="Pinned blurb",
        manually_applied=True,
        kavita_series_id=42,
        kavita_chapter_id=7,
    )
    ov = load_applied_ebook_override(book_dir / "Book.epub")
    assert ov is not None
    assert ov["title"] == "Pinned Title"
    assert ov["author"] == "Pinned Author"
    assert ov["series"] == "Pinned Series"
    assert ov["series_index"] == "2"
    assert ov["summary"] == "Pinned blurb"


def test_apply_ebook_override_fields_prefers_sidecar():
    item = {
        "title": "Filename Stem",
        "author": "Kavita Author",
        "seriesName": "",
        "sequence": "",
        "coverUrl": "/api/library/reader/cover/ebook?seriesId=1",
    }
    apply_ebook_override_fields(
        item,
        {
            "title": "Real Title",
            "author": "Real Author",
            "series": "Real Series",
            "series_index": "3",
            "summary": "Blurb",
            "cover_url": "https://example.com/c.jpg",
        },
        multi_volume=True,
    )
    assert item["title"] == "Real Title"
    assert item["author"] == "Real Author"
    assert item["seriesName"] == "Real Series"
    assert item["sequence"] == "3"
    assert item["description"] == "Blurb"
    assert item["coverUrl"] == "https://example.com/c.jpg"
    assert item["metadataOverride"] is True
