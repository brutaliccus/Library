"""Library Sweep folder cleanup: classification + preview/apply flow."""

from __future__ import annotations

from pathlib import Path
import os
import time

import pytest

from app.services import library_folder_cleanup as cleanup


def test_canonical_layout_docs_has_both_media_types():
    docs = cleanup.canonical_layout_docs()
    assert "audiobook" in docs
    assert "ebook" in docs
    assert "book_layout" in docs["audiobook"]
    assert "book_layout" in docs["ebook"]


def test_classify_audiobook_dir_duplicate_m4b(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    keeper = book / "Title.m4b"
    dupe = book / "Title copy.m4b"
    dupe.write_bytes(b"x" * 10)
    keeper.write_bytes(b"x" * 100)
    # Newest mtime = keeper (delete older copy by default)
    st = dupe.stat()
    os.utime(dupe, (st.st_atime, st.st_mtime - 1000))

    candidates = cleanup.classify_audiobook_dir(book)
    kinds = {c.kind for c in candidates}
    assert "duplicate_m4b" in kinds
    dup_paths = {c.path for c in candidates if c.kind == "duplicate_m4b"}
    assert str(dupe) in dup_paths
    assert str(keeper) in dup_paths
    by_path = {c.path: c for c in candidates if c.kind == "duplicate_m4b"}
    assert by_path[str(dupe)].default_selected is True
    assert by_path[str(dupe)].role == "delete"
    assert by_path[str(keeper)].default_selected is False
    assert by_path[str(keeper)].role == "keep"
    assert all(c.scope == "audiobook" for c in candidates)


def test_classify_audiobook_dir_multipart_leftover_with_m4b(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    (book / "Title.m4b").write_bytes(b"x" * 100)
    (book / "01.mp3").write_bytes(b"x" * 10)

    candidates = cleanup.classify_audiobook_dir(book)
    kinds = [c.kind for c in candidates]
    assert "multipart_leftover" in kinds


def test_classify_audiobook_dir_keeps_sidecars_and_covers(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    (book / "Title.m4b").write_bytes(b"x")
    (book / "metadata.json").write_text("{}")
    (book / "cover.jpg").write_bytes(b"x")
    (book / "book.nfo").write_text("info")

    candidates = cleanup.classify_audiobook_dir(book)
    assert candidates == []


def test_classify_audiobook_dir_junk_file(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    (book / "Title.m4b").write_bytes(b"x")
    (book / "Thumbs.db").write_bytes(b"x")

    candidates = cleanup.classify_audiobook_dir(book)
    assert any(c.kind == "junk_file" for c in candidates)


def test_classify_ebook_dir_numbered_duplicate(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    keeper = book / "Title.epub"
    keeper.write_bytes(b"x" * 100)
    dupe = book / "Title (1).epub"
    dupe.write_bytes(b"x" * 10)

    candidates = cleanup.classify_ebook_dir(book)
    kinds = {c.kind for c in candidates}
    assert "numbered_duplicate" in kinds
    dup_paths = {c.path for c in candidates if c.kind == "numbered_duplicate"}
    assert str(dupe) in dup_paths
    assert all(c.scope == "ebook" for c in candidates)


def test_classify_ebook_dir_keeps_sidecar_and_single_book(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    (book / "Title.epub").write_bytes(b"x")
    (book / "ebook_applied.json").write_text("{}")
    (book / "cover.jpg").write_bytes(b"x")

    candidates = cleanup.classify_ebook_dir(book)
    assert candidates == []


def test_classify_ebook_dir_junk_file(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    (book / "Title.epub").write_bytes(b"x")
    (book / "desktop.ini").write_bytes(b"x")

    candidates = cleanup.classify_ebook_dir(book)
    assert any(c.kind == "junk_file" for c in candidates)


def test_iter_ebook_book_dirs_finds_ebook_and_applied_json(tmp_path: Path):
    root = tmp_path / "ebooks"
    with_ebook = root / "Author" / "Has Ebook"
    with_ebook.mkdir(parents=True)
    (with_ebook / "book.epub").write_bytes(b"x")

    with_applied = root / "Author" / "Applied Only"
    with_applied.mkdir(parents=True)
    (with_applied / "ebook_applied.json").write_text("{}")

    empty_dir = root / "Author" / "Empty"
    empty_dir.mkdir(parents=True)

    dirs = {p for p in cleanup.iter_ebook_book_dirs(root)}
    assert with_ebook in dirs
    assert with_applied in dirs
    assert empty_dir not in dirs


def test_iter_ebook_book_dirs_skips_staging(tmp_path: Path):
    from app.services.ebook_pipeline import ebook_staging_dirname

    root = tmp_path / "ebooks"
    staging = root / ebook_staging_dirname() / "in_progress"
    staging.mkdir(parents=True)
    (staging / "book.epub").write_bytes(b"x")

    dirs = set(cleanup.iter_ebook_book_dirs(root))
    assert staging not in dirs


def test_iter_audiobook_book_dirs_skips_unorganized(tmp_path: Path):
    from app.services.forge_pipeline import unorganized_dirnames

    root = tmp_path / "audiobooks"
    staging_name = sorted(unorganized_dirnames())[0]
    staging = root / staging_name / "in_progress"
    staging.mkdir(parents=True)
    (staging / "book.m4b").write_bytes(b"x")

    real = root / "Author" / "Title"
    real.mkdir(parents=True)
    (real / "book.m4b").write_bytes(b"x")

    dirs = set(cleanup.iter_audiobook_book_dirs(root))
    assert staging not in dirs
    assert real in dirs


def test_classify_library_orphans_defaults_to_both_scopes(tmp_path: Path):
    ab_root = tmp_path / "audiobooks"
    eb_root = tmp_path / "ebooks"
    ab_root.mkdir()
    eb_root.mkdir()
    ab_book = ab_root / "Author" / "Title"
    ab_book.mkdir(parents=True)
    (ab_book / "Title.m4b").write_bytes(b"x" * 10)
    (ab_book / "Title copy.m4b").write_bytes(b"x")

    eb_book = eb_root / "Author" / "Title"
    eb_book.mkdir(parents=True)
    (eb_book / "Title.epub").write_bytes(b"x" * 10)
    (eb_book / "Title (1).epub").write_bytes(b"x")

    preview = cleanup.classify_library_orphans(
        audiobook_root=ab_root, ebook_root=eb_root
    )
    assert set(preview.scopes) == {"audiobook", "ebook"}
    kinds = {(c.kind, c.scope) for c in preview.candidates}
    assert ("duplicate_m4b", "audiobook") in kinds
    assert ("numbered_duplicate", "ebook") in kinds
    assert preview.token


def test_classify_library_orphans_scope_filter(tmp_path: Path):
    ab_root = tmp_path / "audiobooks"
    eb_root = tmp_path / "ebooks"
    ab_root.mkdir()
    eb_root.mkdir()
    eb_book = eb_root / "Author" / "Title"
    eb_book.mkdir(parents=True)
    (eb_book / "Title.epub").write_bytes(b"x")
    (eb_book / "junk.tmp").write_bytes(b"x")

    preview = cleanup.classify_library_orphans(
        scopes=["ebook"], audiobook_root=ab_root, ebook_root=eb_root
    )
    assert preview.scopes == ["ebook"]
    assert all(c.scope == "ebook" for c in preview.candidates)


def test_preview_to_dict_shape(tmp_path: Path):
    ab_root = tmp_path / "audiobooks"
    eb_root = tmp_path / "ebooks"
    ab_root.mkdir()
    eb_root.mkdir()
    preview = cleanup.classify_library_orphans(
        audiobook_root=ab_root, ebook_root=eb_root
    )
    data = preview.to_dict()
    for key in (
        "token",
        "scopes",
        "protected_roots",
        "count",
        "total_bytes",
        "candidates",
        "canonical",
        "expires_in_seconds",
    ):
        assert key in data


def test_apply_cleanup_deletes_previewed_files(tmp_path: Path, monkeypatch):
    ab_root = tmp_path / "audiobooks"
    eb_root = tmp_path / "ebooks"
    ab_root.mkdir()
    eb_root.mkdir()
    monkeypatch.setattr(cleanup.settings, "audiobook_dir", str(ab_root))
    monkeypatch.setattr(cleanup.settings, "ebook_dir", str(eb_root))

    book = ab_root / "Author" / "Title"
    book.mkdir(parents=True)
    keeper = book / "Title.m4b"
    keeper.write_bytes(b"x" * 100)
    dupe = book / "Title copy.m4b"
    dupe.write_bytes(b"x")

    preview = cleanup.classify_library_orphans(
        scopes=["audiobook"], audiobook_root=ab_root, ebook_root=eb_root
    )
    assert any(c.path == str(dupe) for c in preview.candidates)

    result = cleanup.apply_cleanup(token=preview.token, paths=[str(dupe)])
    assert result["ok"] is True
    assert str(dupe) in result["deleted"]
    assert not dupe.exists()
    assert keeper.exists()


def test_apply_cleanup_unknown_token_raises():
    with pytest.raises(ValueError):
        cleanup.apply_cleanup(token="not-a-real-token")


def test_apply_cleanup_refuses_path_outside_preview(tmp_path: Path, monkeypatch):
    ab_root = tmp_path / "audiobooks"
    eb_root = tmp_path / "ebooks"
    ab_root.mkdir()
    eb_root.mkdir()
    monkeypatch.setattr(cleanup.settings, "audiobook_dir", str(ab_root))
    monkeypatch.setattr(cleanup.settings, "ebook_dir", str(eb_root))

    book = ab_root / "Author" / "Title"
    book.mkdir(parents=True)
    (book / "Title.m4b").write_bytes(b"x" * 100)
    (book / "Title copy.m4b").write_bytes(b"x")

    preview = cleanup.classify_library_orphans(
        scopes=["audiobook"], audiobook_root=ab_root, ebook_root=eb_root
    )
    sneaky = tmp_path / "outside.txt"
    sneaky.write_text("nope")
    result = cleanup.apply_cleanup(token=preview.token, paths=[str(sneaky)])
    assert result["ok"] is False
    assert result["errors"]
    assert sneaky.exists()


def test_pick_m4b_keeper_prefers_newest(tmp_path: Path):
    book = tmp_path / "Author" / "Title"
    book.mkdir(parents=True)
    older = book / "Title older.m4b"
    newer = book / "Title.m4b"
    older.write_bytes(b"x" * 50)
    newer.write_bytes(b"x" * 10)  # smaller but newer — still keeper
    older_stat = older.stat()
    os.utime(older, (older_stat.st_atime, older_stat.st_mtime - 1000))

    keeper = cleanup.pick_m4b_keeper([older, newer])
    assert keeper == newer

    candidates = cleanup.classify_audiobook_dir(book)
    dups = [c for c in candidates if c.kind == "duplicate_m4b"]
    selected = {c.path for c in dups if c.default_selected}
    kept = {c.path for c in dups if c.role == "keep"}
    assert str(older) in selected
    assert str(newer) in kept
    assert str(newer) not in selected
