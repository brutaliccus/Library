"""Unit tests for path-safe library media deletion."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.services.ebook_pipeline import EBOOK_UNORGANIZED_DIRNAME
from app.services.forge_pipeline import LEGACY_UNORGANIZED_DIRNAME, UNORGANIZED_DIRNAME
from app.services.library_media_delete import (
    ABS_FORBIDDEN_DIRNAMES,
    EBOOK_FORBIDDEN_DIRNAMES,
    delete_tree_under_library,
    resolve_abs_book_dir,
    resolve_ebook_book_dirs,
)


def test_resolve_abs_book_dir_from_rel_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book = root / "Author" / "Title"
        book.mkdir(parents=True)
        (book / "book.m4b").write_bytes(b"x")
        item = {"relPath": "Author/Title"}
        assert resolve_abs_book_dir(root, item) == book.resolve()


def test_reject_missing_rel_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(ValueError, match="Missing relPath"):
            resolve_abs_book_dir(root, {"relPath": ""})


def test_reject_delete_audiobook_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(ValueError, match="library root"):
            resolve_abs_book_dir(root, {"relPath": ".", "path": str(root)})


def test_reject_unorganized_audiobook_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in (UNORGANIZED_DIRNAME, LEGACY_UNORGANIZED_DIRNAME):
            rel = f"{name}/Title"
            with pytest.raises(ValueError, match="protected"):
                resolve_abs_book_dir(root, {"relPath": rel})


def test_reject_unorganized_ebook_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad = root / EBOOK_UNORGANIZED_DIRNAME / "book.epub"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"x")
        with pytest.raises(ValueError, match="protected"):
            resolve_ebook_book_dirs(root, [bad])


def test_delete_tree_prunes_empty_parents():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        author = root / "Author"
        book = author / "Title"
        book.mkdir(parents=True)
        (book / "book.m4b").write_bytes(b"x")
        delete_tree_under_library(book, root, ABS_FORBIDDEN_DIRNAMES)
        assert not book.exists()
        assert not author.exists()
        assert root.exists()
