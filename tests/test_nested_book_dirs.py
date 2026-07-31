"""Nested whole-book folders must not become tracks of the parent."""

from pathlib import Path

from app.services.pipeline import (
    _collect_book_dirs,
    _looks_like_whole_book_dir,
    _split_collection,
)


def _touch(path: Path, size: int = 64) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)


def test_looks_like_whole_book_dir_nested_m4b(tmp_path: Path) -> None:
    nested = tmp_path / "Timeline"
    _touch(nested / "other.m4b")
    assert _looks_like_whole_book_dir(nested)


def test_looks_like_whole_book_dir_skips_chapter_and_format(tmp_path: Path) -> None:
    ch = tmp_path / "Chapter 01"
    _touch(ch / "track.mp3")
    assert not _looks_like_whole_book_dir(ch)

    fmt = tmp_path / "mp3"
    _touch(fmt / "a.mp3")
    assert not _looks_like_whole_book_dir(fmt)


def test_collect_book_dirs_splits_nested_m4b(tmp_path: Path) -> None:
    book = tmp_path / "Timeline"
    _touch(book / "Timeline.m4b")
    nested = book / "Timeline"
    _touch(nested / "Another Title.m4b")

    found = _collect_book_dirs(book)
    assert len(found) >= 2
    assert any(p.resolve() == book.resolve() or p.parent.resolve() == book.resolve() for p in found)


def test_split_collection_moves_nested_out(tmp_path: Path) -> None:
    author = tmp_path / "Author"
    dest = author / "Timeline"
    _touch(dest / "Timeline.m4b")
    _touch(dest / "Nested Book" / "Nested Book.m4b")

    results = _split_collection(dest, author="Author")
    assert len(results) >= 2
    remaining_m4bs = list(author.rglob("*.m4b"))
    assert len(remaining_m4bs) >= 2
    parents = {p.parent.resolve() for p in remaining_m4bs}
    assert len(parents) >= 2