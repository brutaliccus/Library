"""Unit tests for ebook search title cleaning and rename helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ebook_pipeline import (
    clean_ebook_search_title,
    rename_ebook_to_metadata_title,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Some Book_ (12)", "Some Book"),
        ("Some Book (12)", "Some Book"),
        ("Some_Book_Title", "Some Book Title"),
        ("Timeline (2)", "Timeline"),
        ("Title_2", "Title"),
        ("Title-copy", "Title"),
        ("Book Title [EPUB]", "Book Title"),
        ("Book Title.epub", "Book Title"),
        ("1984", "1984"),
        ("Catch-22", "Catch-22"),
        ("Ready Player One 12", "Ready Player One"),
        ("  Weird__Title_ (3)  ", "Weird Title"),
    ],
)
def test_clean_ebook_search_title(raw: str, expected: str):
    assert clean_ebook_search_title(raw) == expected


def test_rename_ebook_to_metadata_title(tmp_path: Path):
    src = tmp_path / "junk_name_ (12).epub"
    src.write_bytes(b"epub-bytes")
    dest = rename_ebook_to_metadata_title(src, "Clean Title")
    assert dest.name == "Clean Title.epub"
    assert dest.exists()
    assert not src.exists()


def test_rename_ebook_collision_same_size(tmp_path: Path):
    existing = tmp_path / "Clean Title.epub"
    existing.write_bytes(b"same")
    src = tmp_path / "old.epub"
    src.write_bytes(b"same")
    dest = rename_ebook_to_metadata_title(src, "Clean Title")
    assert dest == existing
    assert not src.exists()
