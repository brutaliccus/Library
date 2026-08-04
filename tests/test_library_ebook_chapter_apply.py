"""Tests for chapter-scoped library ebook metadata apply."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.library_metadata_review import (
    LibraryMetadataReviewError,
    _pick_target_ebook,
)


def test_pick_target_requires_disambiguation_for_multi_file():
    paths = [
        Path("/ebooks/A/Book 1/Book 1.epub"),
        Path("/ebooks/A/Book 2/Book 2.epub"),
    ]
    with pytest.raises(LibraryMetadataReviewError):
        _pick_target_ebook(paths, chapter_id=None)


def test_pick_target_by_filename_among_siblings():
    paths = [
        Path("Book 1.epub"),
        Path("Book 2.epub"),
        Path("Book 3.epub"),
    ]
    picked = _pick_target_ebook(paths, chapter_id=189, target_filename="Book 2.epub")
    assert picked.name == "Book 2.epub"


def test_pick_target_by_stem():
    paths = [Path("The Burning Witch 3.epub"), Path("The Burning Witch.epub")]
    picked = _pick_target_ebook(
        paths, chapter_id=189, target_filename="The Burning Witch 3"
    )
    assert picked.name == "The Burning Witch 3.epub"