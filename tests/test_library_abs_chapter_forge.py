"""Tests for in-library audiobook Chapter Forge (Edit Metadata path)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services import library_metadata_review as lmr
from app.services.library_metadata_review import LibraryMetadataReviewError


def _make_book_dir(tmp_path: Path, *, with_m4b: bool = True) -> Path:
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    if with_m4b:
        (book / "Title.m4b").write_bytes(b"fake-m4b")
    else:
        (book / "01.mp3").write_bytes(b"fake-mp3")
    return book


def test_preview_abs_requires_asin(tmp_path: Path):
    book = _make_book_dir(tmp_path)

    async def _run():
        with (
            patch.object(lmr, "_resolve_abs_book_dir", new=AsyncMock(return_value=({}, book))),
            patch.object(lmr, "extract_asin_from_staging", return_value=""),
        ):
            with pytest.raises(LibraryMetadataReviewError, match="ASIN"):
                await lmr.preview_abs_audible_chapters("item-1", asin="")

    asyncio.run(_run())


def test_preview_abs_requires_m4b(tmp_path: Path):
    book = _make_book_dir(tmp_path, with_m4b=False)

    async def _run():
        with patch.object(
            lmr, "_resolve_abs_book_dir", new=AsyncMock(return_value=({}, book))
        ):
            with pytest.raises(LibraryMetadataReviewError, match="No \\.m4b"):
                await lmr.preview_abs_audible_chapters("item-1", asin="B00TEST001")

    asyncio.run(_run())


def test_preview_abs_returns_audible_and_current(tmp_path: Path):
    book = _make_book_dir(tmp_path)
    report = {
        "status": "completed",
        "stats": {
            "asin": "B00TEST001",
            "chapters": 2,
            "backend": "audible-chapters",
            "chaptering_result": {
                "chapters": [
                    {"title": "Opening", "start": 0},
                    {"title": "Chapter 1", "start": 120},
                ]
            },
        },
    }
    loaded = {
        "result": {
            "chapters": [{"title": "Old", "start": 0}],
        }
    }

    async def _run():
        with (
            patch.object(lmr, "_resolve_abs_book_dir", new=AsyncMock(return_value=({}, book))),
            patch.object(lmr.libraforge, "chaptering_load", new=AsyncMock(return_value=loaded)),
            patch.object(
                lmr.libraforge, "start_chaptering_run", new=AsyncMock(return_value="run-1")
            ),
            patch.object(lmr.libraforge, "wait_for_run", new=AsyncMock(return_value=report)),
            patch.object(lmr.libraforge, "run_failed", return_value=False),
        ):
            return await lmr.preview_abs_audible_chapters("item-1", asin="B00TEST001")

    result = asyncio.run(_run())
    assert result["ok"] is True
    assert result["asin"] == "B00TEST001"
    assert result["chapter_count"] == 2
    assert len(result["chapters"]) == 2
    assert result["chapters"][0]["title"] == "Opening"
    assert result["current_chapter_count"] == 1
    assert (book / "chapter_preview.json").is_file()


def test_apply_abs_embeds_and_syncs_abs(tmp_path: Path):
    book = _make_book_dir(tmp_path)
    report = {
        "status": "completed",
        "stats": {
            "asin": "B00TEST001",
            "chapters": 2,
            "chaptering_result": {
                "chapters": [
                    {"title": "Opening", "start": 0},
                    {"title": "Chapter 1", "start": 120},
                ],
                "duration": 240.0,
            },
        },
    }

    async def _run():
        with (
            patch.object(lmr, "_resolve_abs_book_dir", new=AsyncMock(return_value=({}, book))),
            patch.object(
                lmr.libraforge, "start_chaptering_run", new=AsyncMock(return_value="run-2")
            ) as start,
            patch.object(lmr.libraforge, "wait_for_run", new=AsyncMock(return_value=report)),
            patch.object(lmr.libraforge, "run_failed", return_value=False),
            patch.object(
                lmr.chapter_embed,
                "embed_chapters_into_audio",
                return_value=book / "Title.m4b",
            ) as embed,
            patch.object(
                lmr.audiobookshelf,
                "sync_book_dir_metadata_to_abs",
                new=AsyncMock(return_value={"chapters_updated": True, "updated": False}),
            ) as sync,
            patch.object(lmr.audiobookshelf, "invalidate_cache") as invalidate,
        ):
            result = await lmr.apply_abs_audible_chapters("item-1", asin="B00TEST001")
            return result, start, embed, sync, invalidate

    result, start, embed, sync, invalidate = asyncio.run(_run())
    assert result["ok"] is True
    assert result["embedded"] is True
    assert result["chapter_count"] == 2
    assert "Embedded Audible chapters" in result["status_detail"]
    start.assert_awaited_once()
    assert start.await_args.kwargs["backend"] == "audible-chapters"
    assert start.await_args.kwargs.get("no_save") in (None, False)
    embed.assert_called_once()
    sync.assert_awaited_once()
    invalidate.assert_called_once()


def test_apply_abs_raises_when_no_chapters(tmp_path: Path):
    book = _make_book_dir(tmp_path)
    report = {"status": "completed", "stats": {"chapters": 0, "chaptering_result": {}}}

    async def _run():
        with (
            patch.object(lmr, "_resolve_abs_book_dir", new=AsyncMock(return_value=({}, book))),
            patch.object(
                lmr.libraforge, "start_chaptering_run", new=AsyncMock(return_value="run-3")
            ),
            patch.object(lmr.libraforge, "wait_for_run", new=AsyncMock(return_value=report)),
            patch.object(lmr.libraforge, "run_failed", return_value=False),
            patch.object(lmr.chapter_embed, "chapters_from_run_report", return_value=[]),
            patch.object(lmr.chapter_embed, "chapters_from_libraforge_sidecar", return_value=[]),
        ):
            with pytest.raises(LibraryMetadataReviewError, match="no chapters"):
                await lmr.apply_abs_audible_chapters("item-1", asin="B00TEST001")

    asyncio.run(_run())
