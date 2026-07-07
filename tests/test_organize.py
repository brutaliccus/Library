"""Tests for the audiobook organize pipeline: folder-per-chapter flattening,
collection splitting, and ABS metadata.json sidecar writing."""

import json

import pytest

from app.services.pipeline import (
    _looks_like_chapter_folder,
    _title_from_folder,
    organize_audiobook_files,
)


def _make_audio(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 16)


# ---------------- chapter folder detection ----------------

@pytest.mark.parametrize("name", [
    "Chapter 01",
    "chapter 12",
    "CH 3",
    "Part 2",
    "Disc 1",
    "CD2",
    "Track 07",
    "01",
    "003",
    "01 - Opening Credits",
    "02-chapter-1",
    "05- Audible Intro",
    "Dungeon Crawler Carl Book 8 - 001 - Chapter 1",
    "The Book - Part 3",
    "Chapter Seven",
])
def test_chapter_folder_names_detected(name):
    assert _looks_like_chapter_folder(name), name


@pytest.mark.parametrize("name", [
    "Dungeon Crawler Carl",
    "The Way of Kings",
    "Project Hail Mary (Unabridged)",
])
def test_book_folder_names_not_detected(name):
    assert not _looks_like_chapter_folder(name), name


# ---------------- folder-per-chapter flatten (the ABS duplicate-book bug) ----------------

def test_chapter_per_folder_flattens_to_one_book(tmp_path):
    book = tmp_path / "Some Author" / "Great Book"
    for i in range(1, 31):
        _make_audio(book / f"Chapter {i:02d}" / f"chapter_{i:02d}.mp3")

    dirs = organize_audiobook_files(book, "Some Author")

    assert dirs == [book]
    mp3s = list(book.glob("*.mp3"))
    assert len(mp3s) == 30
    # No chapter subdirectories remain to be mistaken for separate books
    assert not [d for d in book.iterdir() if d.is_dir()]


def test_single_m4b_book_untouched(tmp_path):
    book = tmp_path / "Author" / "Solo Book"
    _make_audio(book / "Solo Book.m4b")

    dirs = organize_audiobook_files(book, "Author")

    assert dirs == [book]
    assert (book / "Solo Book.m4b").exists()


def test_collection_splits_into_books(tmp_path):
    incoming = tmp_path / "Author" / "_incoming_1"
    _make_audio(incoming / "Series 01 - First Book" / "book1.m4b")
    _make_audio(incoming / "Series 02 - Second Book" / "book2.m4b")

    dirs = organize_audiobook_files(incoming, "Author")

    names = sorted(d.name for d in dirs)
    assert names == ["Series 01 - First Book", "Series 02 - Second Book"]
    assert not incoming.exists()


# ---------------- metadata.json sidecar ----------------

def test_metadata_json_written(tmp_path):
    book = tmp_path / "Matt Dinniman" / "Dungeon Crawler Carl"
    _make_audio(book / "Dungeon Crawler Carl.m4b")

    organize_audiobook_files(book, "Matt Dinniman")

    meta = json.loads((book / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Dungeon Crawler Carl"
    assert meta["authors"] == ["Matt Dinniman"]


def test_metadata_json_extracts_series_from_folder(tmp_path):
    book = tmp_path / "Author" / "White Trash Zombie 03 - The Big Finale"
    _make_audio(book / "file.mp3")
    for i in range(5):
        _make_audio(book / f"track_{i}.mp3")

    organize_audiobook_files(book, "Author")

    meta = json.loads((book / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "The Big Finale"
    assert meta["series"] == ["White Trash Zombie #3"]


def test_metadata_json_never_overwritten(tmp_path):
    book = tmp_path / "Author" / "Book"
    _make_audio(book / "book.m4b")
    (book / "metadata.json").write_text('{"title": "User Edited"}', encoding="utf-8")

    organize_audiobook_files(book, "Author")

    meta = json.loads((book / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "User Edited"


def test_metadata_strips_release_junk_from_title(tmp_path):
    assert _title_from_folder("Project Hail Mary (2021) [64k] m4b Unabridged") == "Project Hail Mary"
    assert _title_from_folder("The Martian - Andy Weir mp3 128 kbps") == "The Martian - Andy Weir"
