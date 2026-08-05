"""Ebook Library Sweep: fingerprint/magnet helpers, options, and worker helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.services import library_ebook_sweep, library_ingest


def test_ebook_sweep_fingerprint_and_magnet_helpers():
    assert library_ingest.ebook_sweep_fingerprint("Author/Title") == "ebook:Author/Title"
    assert library_ingest.ebook_sweep_magnet("Author/Title") == "sweep:ebook:Author/Title"


def test_ebook_sweep_fingerprint_is_distinct_from_audiobook_sweep():
    # Same relative key must not collide between audiobook and ebook fingerprints.
    assert library_ingest.ebook_sweep_fingerprint("abc") != library_ingest.sweep_fingerprint(
        "abc"
    )


def test_ebook_sweep_magnet_is_synthetic():
    magnet = library_ingest.ebook_sweep_magnet("Author/Title")
    assert library_ingest.is_synthetic_magnet(magnet)


def test_ebook_sweep_options_defaults(monkeypatch):
    async def _true(_key, default=False):
        return True

    monkeypatch.setattr(
        "app.services.instance_settings.get_effective_bool",
        _true,
    )
    options = asyncio.run(library_ingest.ebook_sweep_options())
    assert options["convert_all_to_epub"] is True
    assert options["force_metadata"] is True
    assert options["provider_order"] == ["hardcover", "open_library"]


def test_ebook_sweep_options_respects_false_overrides(monkeypatch):
    async def _false(_key, default=False):
        return False

    monkeypatch.setattr(
        "app.services.instance_settings.get_effective_bool",
        _false,
    )
    options = asyncio.run(library_ingest.ebook_sweep_options())
    assert options["convert_all_to_epub"] is False
    assert options["force_metadata"] is False


def test_ebook_sweep_medium_constant():
    assert library_ebook_sweep.MEDIUM == "ebook"


def test_kavita_scan_every_helper(monkeypatch):
    monkeypatch.setattr(library_ebook_sweep.settings, "ebook_sweep_kavita_scan_every", 40)
    assert library_ebook_sweep._kavita_scan_every() == 40
    monkeypatch.setattr(library_ebook_sweep.settings, "ebook_sweep_kavita_scan_every", 0)
    assert library_ebook_sweep._kavita_scan_every() == 1
    monkeypatch.setattr(library_ebook_sweep.settings, "ebook_sweep_kavita_scan_every", "nope")
    assert library_ebook_sweep._kavita_scan_every() == 25


def test_rel_posix_relative_and_fallback(tmp_path: Path):
    root = tmp_path / "ebooks"
    book = root / "Author" / "Title"
    book.mkdir(parents=True)
    assert library_ebook_sweep._rel_posix(root, book) == "Author/Title"

    outside = tmp_path / "elsewhere" / "Book"
    outside.mkdir(parents=True)
    # Outside the root: falls back to the folder's own posix path (no crash).
    assert library_ebook_sweep._rel_posix(root, outside) == outside.as_posix()


def test_folder_title_author_hint_author_title_layout(tmp_path: Path):
    root = tmp_path / "ebooks"
    book = root / "Sanderson" / "Mistborn"
    book.mkdir(parents=True)
    title, author = library_ebook_sweep._folder_title_author_hint(root, book)
    assert title == "Mistborn"
    assert author == "Sanderson"


def test_folder_title_author_hint_single_level():
    root = Path("/ebooks")
    book = Path("/ebooks/Standalone Title")
    title, author = library_ebook_sweep._folder_title_author_hint(root, book)
    assert title == "Standalone Title"
    assert author is None


def test_set_current_and_up_next_helpers():
    library_ebook_sweep._set_current()
    assert library_ebook_sweep._current is None

    library_ebook_sweep._set_current(
        request_id=7,
        title="Mistborn",
        author="Sanderson",
        cover_url=None,
        status="metadata_forge",
        book_dir="Sanderson/Mistborn",
    )
    assert library_ebook_sweep._current["title"] == "Mistborn"
    assert library_ebook_sweep._current["request_id"] == 7

    preview = {"title": "Next Book", "author": "Someone", "book_dir": "Someone/Next Book"}
    library_ebook_sweep._set_up_next(preview)
    assert library_ebook_sweep._up_next["title"] == "Next Book"
    library_ebook_sweep._set_up_next(None)
    assert library_ebook_sweep._up_next is None

    library_ebook_sweep._set_current()


def test_book_dir_preview_uses_applied_metadata(tmp_path: Path):
    root = tmp_path / "ebooks"
    book = root / "Sanderson" / "Mistborn"
    book.mkdir(parents=True)

    class _Applied:
        title = "Mistborn: The Final Empire"
        author = "Brandon Sanderson"
        cover_url = "https://example.com/cover.jpg"

    preview = library_ebook_sweep._book_dir_preview(root, book, applied=_Applied())
    assert preview["title"] == "Mistborn: The Final Empire"
    assert preview["author"] == "Brandon Sanderson"
    assert preview["cover_url"] == "https://example.com/cover.jpg"
    assert preview["book_dir"] == "Sanderson/Mistborn"
    assert preview["request_id"] is None


def test_book_dir_preview_without_applied_uses_folder_hint(tmp_path: Path):
    root = tmp_path / "ebooks"
    book = root / "Sanderson" / "Mistborn"
    book.mkdir(parents=True)

    preview = library_ebook_sweep._book_dir_preview(root, book)
    assert preview["title"] == "Mistborn"
    assert preview["author"] == "Sanderson"
    assert preview["cover_url"] is None
