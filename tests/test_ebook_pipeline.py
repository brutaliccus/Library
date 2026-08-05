"""Unit tests for DIY ebook organizer (paths, confidence gate, reject wipe)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ebook_pipeline import (
    EBOOK_UNORGANIZED_DIRNAME,
    EbookMeta,
    ebook_staging_dir,
    ensure_ebook_unorganized_root,
    extract_isbns_from_text,
    final_ebook_path,
    final_ebook_relative_dir,
    identify_ebook_metadata,
    organize_ebook_files,
    pick_primary_ebook,
    title_similarity,
    wipe_staging,
)
from app.services.forge_pipeline import delete_request_staging_tree, resolve_staging_dir


@pytest.fixture(autouse=True)
def _ebook_dirs(tmp_path, monkeypatch):
    ebook = tmp_path / "ebooks"
    audio = tmp_path / "audiobooks"
    ebook.mkdir()
    audio.mkdir()
    monkeypatch.setattr("app.services.ebook_pipeline.settings.ebook_dir", str(ebook))
    monkeypatch.setattr("app.services.ebook_pipeline.settings.ebook_pipeline_enabled", True)
    monkeypatch.setattr("app.services.ebook_pipeline.settings.ebook_min_score", 0.70)
    monkeypatch.setattr("app.services.forge_pipeline.settings.ebook_dir", str(ebook))
    monkeypatch.setattr("app.services.forge_pipeline.settings.audiobook_dir", str(audio))
    return ebook, audio


def test_ebook_staging_under_unorganized(_ebook_dirs):
    ebook, _audio = _ebook_dirs
    path = ebook_staging_dir(7, "Some Book: Title")
    assert path.parent.name == EBOOK_UNORGANIZED_DIRNAME
    assert path.name.startswith("req_7_")
    assert (ebook / "unorganized" / ".ignore").is_file()
    ensure_ebook_unorganized_root()
    assert (ebook / "unorganized").is_dir()


def test_final_layout_with_and_without_series():
    meta = EbookMeta(title="Ash and Quill", author="Rachel Caine", series="Great Library")
    assert final_ebook_relative_dir(meta) == Path("Rachel Caine") / "Great Library" / "Ash and Quill"

    meta2 = EbookMeta(
        title="Ash and Quill",
        author="Rachel Caine",
        series="Great Library",
        edition="Special",
    )
    assert final_ebook_relative_dir(meta2) == (
        Path("Rachel Caine") / "Great Library [Special]" / "Ash and Quill"
    )

    meta3 = EbookMeta(title="Standalone", author="Someone")
    assert final_ebook_relative_dir(meta3) == Path("Someone") / "Standalone"


def test_final_ebook_path_filename(_ebook_dirs):
    ebook, _ = _ebook_dirs
    meta = EbookMeta(title="My Book", author="A Author", series="S")
    path = final_ebook_path(meta, suffix=".epub")
    assert path == ebook / "A Author" / "S" / "My Book" / "My Book.epub"


def test_organize_moves_primary_and_wipe(_ebook_dirs):
    ebook, _ = _ebook_dirs
    staging = ebook_staging_dir(3, "Timeline")
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "noise.txt").write_text("x")
    (staging / "Timeline.epub").write_bytes(b"epub")
    (staging / "Timeline.pdf").write_bytes(b"pdf")

    meta = EbookMeta(title="Timeline", author="Michael Crichton", score=0.95)
    dest = organize_ebook_files(staging, meta)
    assert dest.exists()
    assert dest.name == "Timeline.epub"
    assert dest.parent == ebook / "Michael Crichton" / "Timeline"
    assert not (staging / "Timeline.epub").exists()

    wipe_staging(staging)
    assert not staging.exists()


def test_organize_skips_same_size_and_cleans_duplicates(_ebook_dirs):
    from app.services.ebook_pipeline import ensure_series_index

    ebook, _ = _ebook_dirs
    meta = EbookMeta(
        title="The Burning Witch 2",
        author="Delemhach",
        series="The Burning Witch",
        series_index="2",
        score=0.95,
    )
    dest_dir = ebook / "Delemhach" / "The Burning Witch" / "The Burning Witch 2"
    dest_dir.mkdir(parents=True)
    canonical = dest_dir / "The Burning Witch 2.epub"
    canonical.write_bytes(b"same-bytes")
    (dest_dir / "The Burning Witch 2 (2).epub").write_bytes(b"same-bytes")

    staging = ebook_staging_dir(9, "Burning")
    staging.mkdir(parents=True)
    (staging / "The Burning Witch 2.epub").write_bytes(b"same-bytes")

    out = organize_ebook_files(staging, meta)
    assert out == canonical
    assert not (dest_dir / "The Burning Witch 2 (2).epub").exists()
    assert not (staging / "The Burning Witch 2.epub").exists()

    filled = ensure_series_index(
        EbookMeta(title="The Burning Witch 3", author="Delemhach", series="The Burning Witch")
    )
    assert filled.series_index == "3"


def test_pick_primary_prefers_epub(_ebook_dirs):
    staging = ebook_staging_dir(1, "Book")
    staging.mkdir(parents=True)
    (staging / "a.mobi").write_bytes(b"m")
    (staging / "b.epub").write_bytes(b"e")
    assert pick_primary_ebook(staging).name == "b.epub"


def test_resolve_ebook_staging_docker_style(_ebook_dirs):
    ebook, _ = _ebook_dirs
    staging = ebook / "unorganized" / "req_9_Timeline"
    staging.mkdir(parents=True)
    resolved = resolve_staging_dir("/ebooks/unorganized/req_9_Timeline")
    assert resolved == staging.resolve()
    assert resolve_staging_dir("/mnt/eBooks/unorganized/req_9_Timeline") == staging.resolve()


def test_resolve_rejects_outside_ebook_unorganized(_ebook_dirs):
    ebook, _ = _ebook_dirs
    outside = ebook / "Some Author" / "Title"
    outside.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        resolve_staging_dir(str(outside))


def test_delete_request_staging_tree_ebook_orphan(_ebook_dirs):
    ebook, _ = _ebook_dirs
    unorg = ebook / "unorganized"
    unorg.mkdir()
    kept = unorg / "req_9_Timeline"
    orphan = unorg / "req_9_orphan"
    other = unorg / "req_8_other"
    final = ebook / "Author" / "Book"
    for d in (kept, orphan, other, final):
        d.mkdir(parents=True)
        (d / "f.epub").write_bytes(b"x")

    deleted = delete_request_staging_tree(9, "/ebooks/unorganized/req_9_Timeline")
    deleted_names = {p.name for p in deleted}
    assert "req_9_Timeline" in deleted_names
    assert "req_9_orphan" in deleted_names
    assert other.exists()
    assert final.exists()
    assert (final / "f.epub").exists()


def test_title_similarity_and_isbn_extract():
    assert title_similarity("The Dark Tower", "Dark Tower") > 0.5
    assert title_similarity("Foo", "Bar") == 0.0
    isbns = extract_isbns_from_text("Book 978-0-123456-78-9 epub", "nope")
    assert any(i.startswith("978") or len(i) in (10, 13) for i in isbns)


def test_identify_quarantine_low_score(_ebook_dirs, monkeypatch):
    import asyncio

    staging = ebook_staging_dir(5, "Mystery")
    staging.mkdir(parents=True)
    (staging / "mystery.epub").write_bytes(b"e")

    monkeypatch.setattr(
        "app.services.ebook_pipeline.settings.ebook_min_score",
        0.70,
    )

    async def _run():
        with (
            patch("app.services.google_books.get_catalog_volume", new_callable=AsyncMock) as cat,
            patch("app.services.hardcover.get_api_key", new_callable=AsyncMock) as key,
            patch("app.services.hardcover.search_books", new_callable=AsyncMock) as search,
            patch("app.services.ol_catalog.catalog_ready", return_value=False),
        ):
            cat.return_value = None
            key.return_value = ""
            search.return_value = []
            return await identify_ebook_metadata(
                staging=staging,
                title_hint="Mystery",
                author_hint="Unknown",
                google_volume_id=None,
            )

    meta = asyncio.run(_run())
    assert meta.score < 0.70
    assert meta.source == "hint"


def test_identify_catalog_high_score(_ebook_dirs):
    import asyncio

    staging = ebook_staging_dir(6, "Known")
    staging.mkdir(parents=True)

    async def _run():
        with patch(
            "app.services.google_books.get_catalog_volume",
            new_callable=AsyncMock,
        ) as cat:
            cat.return_value = {
                "title": "Known Book",
                "authors": ["Jane Doe"],
                "seriesName": "Saga",
                "seriesBookNumber": "2",
                "isbn13": "9781234567890",
            }
            return await identify_ebook_metadata(
                staging=staging,
                title_hint="Known Book",
                author_hint="Jane Doe",
                google_volume_id="OL:123",
            )

    meta = asyncio.run(_run())
    assert meta.score >= 0.85
    assert meta.series == "Saga"
    assert meta.source == "catalog"


def test_corrupt_metadata_rejected():
    from app.services.ebook_pipeline import (
        EbookMeta,
        is_corrupt_metadata_value,
        sanitize_ebook_meta,
    )
    from app.utils.book_series import library_series_from_title

    assert is_corrupt_metadata_value("?" * 20)
    assert is_corrupt_metadata_value("?????? ?????? 2")
    assert not is_corrupt_metadata_value("A Game of Thrones")

    meta = sanitize_ebook_meta(
        EbookMeta(
            title="?" * 30,
            author=".caltrash",
            series="?" * 20,
            score=0.99,
            source="hardcover",
        )
    )
    assert meta.title == "Unknown"
    assert meta.author == "Unknown"
    assert meta.series is None
    assert meta.score <= 0.2

    inferred = library_series_from_title("Leviathan Wakes: Book 1 of the Expanse")
    assert inferred is not None
    assert inferred[0].lower() == "expanse"
    assert inferred[1] == "1"


def test_ebook_series_paths_coherent_rejects_mixed_authors(_ebook_dirs):
    from app.services.ebook_pipeline import ebook_series_paths_coherent

    ebook, _ = _ebook_dirs
    a = ebook / "Author A" / "Series" / "Book A" / "a.epub"
    b = ebook / "Author B" / "Series" / "Book B" / "b.epub"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    assert ebook_series_paths_coherent(a, [a])
    assert not ebook_series_paths_coherent(a, [a, b])


def test_ebook_series_paths_coherent_rejects_caltrash_and_author_dump(_ebook_dirs):
    from app.services.ebook_pipeline import (
        ensure_ebook_kavita_ignores,
        ebook_series_paths_coherent,
    )

    ebook, _ = _ebook_dirs
    ensure_ebook_kavita_ignores()
    ignore = ebook / ".kavitaignore"
    assert ignore.is_file()
    text = ignore.read_text(encoding="utf-8")
    assert "caltrash/*" in text
    assert "unorganized/*" in text

    good = ebook / "Matt Dinniman" / "Dungeon Crawler Carl" / "Book1" / "b1.epub"
    good2 = ebook / "Matt Dinniman" / "Dungeon Crawler Carl" / "Book2" / "b2.epub"
    trash = ebook / "caltrash" / "Unknown" / "1" / "1.epub"
    other = ebook / "Matt Dinniman" / "Other Series" / "Book3" / "b3.epub"
    for p in (good, good2, trash, other):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    assert ebook_series_paths_coherent(good, [good, good2])
    assert not ebook_series_paths_coherent(good, [good, trash])
    assert not ebook_series_paths_coherent(good, [good, other])


def test_ensure_series_from_book_n_of_title():
    from app.services.ebook_pipeline import EbookMeta, ensure_series_index

    meta = ensure_series_index(
        EbookMeta(
            title="Leviathan Wakes: Book 1 of the Expanse",
            author="James S. A. Corey",
            score=0.99,
        )
    )
    assert meta.series and "expanse" in meta.series.lower()
    assert meta.series_index == "1"
