"""Unit tests for LibraForge client helpers (no live network)."""

from __future__ import annotations

import json

import pytest

from app.services.libraforge import (
    metadata_already_processed,
    metadata_auto_applied,
    metadata_matched_without_apply,
    organizer_moved_files,
    organizer_move_targets,
    organizer_stale_existing_skips,
    quarantine_reason_from_report,
    run_failed,
)
from app.services.forge_pipeline import (
    audiobook_staging_dir,
    build_staging_tree,
    clean_catalog_title,
    cover_url_from_staging,
    delete_request_staging_tree,
    delete_staging_entry,
    detect_pipeline_state,
    extract_asin_from_staging,
    m4b_source_dirs,
    needs_m4b_conversion,
    normalize_asin,
    primary_audio_for_chaptering,
    read_chapter_preview,
    resolve_resume_from,
    resolve_staging_dir,
    safe_path_under_staging,
    seed_staging_metadata_hints,
    staging_has_applied_metadata,
    write_chapter_preview,
    _cleanup_forge_temps,
    _cleanup_staging_after_folder_forge,
    _extract_chapters_from_report,
    _remove_source_audio_after_m4b,
)


def test_metadata_auto_applied_write_written():
    assert metadata_auto_applied({"files_by_category": {"write:written": [{"path": "/x"}]}})


def test_mode_full_without_write_is_not_applied():
    """Dark Tower race: Pass-1 match must not count as apply."""
    report = {
        "files_by_category": {
            "mode:full": [{"path": "/x"}],
            "status:matched": [{"path": "/x"}],
        },
        "stats": {"mode_breakdown": {"full": 1}, "matched": 1, "skipped": 0},
        "report_items": [
            {
                "path": "/x",
                "status": "matched",
                "score": 1.0,
                "mode": "full",
                "write_action": "",
                "match": {"title": "Dark Tower I"},
            }
        ],
    }
    assert not metadata_auto_applied(report)
    assert metadata_matched_without_apply(report)
    reason = quarantine_reason_from_report(report)
    assert "did not apply" in reason.lower() or "write_action" in reason


def test_metadata_not_applied_when_matched_but_write_skipped():
    """status:matched + write skipped must quarantine (Harry Potter full-cast case)."""
    report = {
        "files_by_category": {
            "mode:none": [{"path": "/x"}],
            "status:matched": [{"path": "/x"}],
            "status:skipped": [{"path": "/x"}],
            "write:write_skipped": [{"path": "/x"}],
        },
        "stats": {
            "matched": 0,
            "skipped": 7,
            "mode_breakdown": {"full": 0, "none": 1},
            "skip_reasons": {"no usable Audible match": 6, "score below minimum: 0.4038 < 0.7": 1},
        },
        "manual_review_items": [{"path": "/x", "reasons": ["no match"]}],
        "report_items": [
            {
                "path": "/x",
                "status": "skipped",
                "score": 0.4038,
                "write_action": "write_skipped",
                "skip_reason": "skipped: score below minimum: 0.4038 < 0.7",
            }
        ],
    }
    assert not metadata_auto_applied(report)
    reason = quarantine_reason_from_report(report)
    assert "score below minimum" in reason or "did not auto-apply" in reason


def test_metadata_already_processed_detected():
    report = {
        "stats": {
            "found": 1,
            "matched": 0,
            "skipped": 1,
            "skip_reasons": {"already processed": 1},
            "mode_breakdown": {"full": 0},
        },
        "report_items": [
            {
                "path": "/audiobooks/.unorganized/req_1/book.m4b",
                "status": "skipped",
                "skip_reason": "already processed",
                "score": 0.0,
                "write_action": "write_skipped",
                "title": "already processed",
                "local": {"title": "Book", "author": "Author", "asin": "B0ABCDEF12"},
            }
        ],
        "categories": {"status:skipped": [1], "write:write_skipped": [1]},
    }
    assert metadata_already_processed(report)
    assert not metadata_auto_applied(report)


def test_metadata_already_processed_not_confused_with_low_score():
    report = {
        "stats": {"skip_reasons": {"score below minimum: 0.4 < 0.7": 1}},
        "report_items": [
            {"skip_reason": "skipped: score below minimum: 0.4 < 0.7", "status": "skipped"}
        ],
    }
    assert not metadata_already_processed(report)


def test_metadata_auto_applied_from_report_items_written():
    assert metadata_auto_applied(
        {
            "stats": {"mode_breakdown": {"full": 1}, "matched": 1, "skipped": 0},
            "report_items": [
                {"path": "/x", "status": "matched", "write_action": "written", "score": 1.0}
            ],
        }
    )


def test_metadata_not_applied_when_empty():
    assert not metadata_auto_applied({"files_by_category": {"mode:none": [{"path": "/x"}]}})


def test_run_failed_on_error_status():
    assert run_failed({"status": "failed", "error": "boom"})
    assert not run_failed({"status": "completed", "returncode": 0})


def test_organizer_moved_files():
    assert organizer_moved_files({"stats": {"moves_succeeded": 1}})
    assert not organizer_moved_files({"stats": {"moves_succeeded": 0, "move_items": []}})
    # Skipped review stubs in move_items must not count as successful moves
    # (Honeycraves / Honeybloods false-complete wiped staging).
    skipped_report = {
        "stats": {
            "moves_succeeded": 0,
            "planned_moves": 0,
            "move_items": [
                {
                    "title": "Honeycraves Honeybloods",
                    "author": "Unknown Author",
                    "structure": "skipped_unknown_author",
                    "review_reasons": ["skipped unknown author"],
                    "source": "/audiobooks/.unorganized/req_36_x/book.m4b",
                    "target": "/audiobooks/Unknown Author/Honeycraves Honeybloods/Honeycraves Honeybloods",
                }
            ],
        }
    }
    assert not organizer_moved_files(skipped_report)
    # Legacy reports without moves_succeeded still treat real targets as moved.
    assert organizer_moved_files(
        {
            "stats": {
                "move_items": [
                    {
                        "title": "Honeybites",
                        "structure": "moved",
                        "target": "/audiobooks/I.S. Belle/Honeybloods/Honeybites",
                    }
                ]
            }
        }
    )
    assert not organizer_moved_files(
        {
            "stats": {
                "move_items": [
                    {
                        "structure": "skipped_existing_book_folders",
                        "review_reasons": [
                            "skipped: folder name already matches the naming template"
                        ],
                        "target": "/audiobooks/I.S. Belle/Honeybloods/Honeybites",
                    }
                ]
            }
        }
    )


def test_organizer_move_targets():
    report = {
        "stats": {
            "moves_succeeded": 1,
            "move_items": [
                {"target": "/audiobooks/Author/Title", "source": "/audiobooks/.unorganized/x"},
                {"target": "/audiobooks/Author/Title"},  # dedupe
                {"target": ""},
            ],
        }
    }
    assert organizer_move_targets(report) == ["/audiobooks/Author/Title"]
    assert organizer_move_targets({}) == []


def test_organizer_stale_existing_skips():
    report = {
        "stats": {
            "moves_succeeded": 0,
            "move_items": [
                {
                    "structure": "skipped_existing_book_folders",
                    "review_reasons": [
                        "title matches series name; using sequence only",
                        "skipped: folder name already matches the naming template",
                    ],
                    "source": "/audiobooks/.unorganized/req_34_Honeybloods/Honeybloods/book.m4b",
                    "target": "/audiobooks/I.S. Belle/Honeybloods/Honeybloods",
                },
                {
                    "structure": "skipped_unknown_author",
                    "source": "/audiobooks/.unorganized/x/a.m4b",
                    "target": "/audiobooks/Unknown/a",
                },
            ],
        }
    }
    stale = organizer_stale_existing_skips(report)
    assert len(stale) == 1
    assert stale[0]["target"].endswith("/Honeybloods")


def test_force_apply_stale_organizer_skips(tmp_path, monkeypatch):
    from app.services import forge_pipeline

    library = tmp_path / "audiobooks"
    staging = library / ".unorganized" / "req_34_Honeybloods"
    book_dir = staging / "Honeybloods"
    book_dir.mkdir(parents=True)
    src = book_dir / "Honeybloods- Book 1.m4b"
    src.write_bytes(b"audio")
    (book_dir / "libraforge.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(library))
    report = {
        "stats": {
            "moves_succeeded": 0,
            "move_items": [
                {
                    "structure": "skipped_existing_book_folders",
                    "review_reasons": [
                        "skipped: folder name already matches the naming template"
                    ],
                    "source": str(src),
                    "target": str(library / "I.S. Belle" / "Honeybloods" / "Honeybloods"),
                }
            ],
        }
    }
    n = forge_pipeline._force_apply_stale_organizer_skips(staging, report)
    assert n == 1
    dest = library / "I.S. Belle" / "Honeybloods" / "Honeybloods" / src.name
    assert dest.is_file()
    assert dest.read_bytes() == b"audio"
    assert not src.exists()
    assert (dest.parent / "libraforge.json").is_file()


def test_quarantine_reason_from_manual_items():
    reason = quarantine_reason_from_report(
        {"manual_review_items": [{"reasons": ["low score", "mode:none"], "path": "/a"}]}
    )
    assert "low score" in reason


def test_staging_dir_under_unorganized(tmp_path, monkeypatch):
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    path = audiobook_staging_dir(42, "Some Book: Title")
    assert path.parent.name == ".unorganized"
    assert path.name.startswith("req_42_")
    assert (tmp_path / ".unorganized" / ".ignore").is_file()


def test_find_library_book_dir_skips_series_container(tmp_path, monkeypatch):
    """Series folder named like book 1 must not win over a missing book folder."""
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    series = tmp_path / "I.S. Belle" / "Honeybloods"
    bites = series / "Honeybites"
    bites.mkdir(parents=True)
    (bites / "Honeybites - Honeybloods, Book 2.m4b").write_bytes(b"x")
    # No book-1 folder — only the series container + book 2.
    assert forge_pipeline.find_library_book_dir("Honeybloods", "I.S. Belle") is None
    assert forge_pipeline.find_library_book_dir("Honeybites", "I.S. Belle") == bites
    # Book folder directly under series still matches.
    book1 = series / "Honeybloods"
    book1.mkdir()
    (book1 / "Honeybloods - Honeybloods, Book 1.m4b").write_bytes(b"y")
    assert forge_pipeline.find_library_book_dir("Honeybloods", "I.S. Belle") == book1


def test_needs_m4b_single_m4b(tmp_path):
    book = tmp_path / "book"
    book.mkdir()
    (book / "Title.m4b").write_bytes(b"x")
    assert needs_m4b_conversion(book) is False


def test_needs_m4b_does_not_merge_multiple_complete_m4bs(tmp_path):
    """Series packs of whole .m4b files must never be concatenated."""
    book = tmp_path / "pack"
    book.mkdir()
    (book / "Book One.m4b").write_bytes(b"a")
    (book / "Book Two.m4b").write_bytes(b"b")
    (book / "Book Three.m4b").write_bytes(b"c")
    assert needs_m4b_conversion(book) is False


def test_needs_m4b_multipart_mp3(tmp_path):
    book = tmp_path / "book"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"a")
    (book / "02.mp3").write_bytes(b"b")
    assert needs_m4b_conversion(book) is True


def test_needs_m4b_nested_multipart(tmp_path):
    """Timeline-style: parts live under mp3/, not staging root."""
    staging = tmp_path / "req_152"
    mp3 = staging / "mp3"
    mp3.mkdir(parents=True)
    (mp3 / "Tape1.mp3").write_bytes(b"a")
    (mp3 / "Tape2.mp3").write_bytes(b"b")
    assert needs_m4b_conversion(staging) is True
    assert m4b_source_dirs(staging) == [mp3]


def test_m4b_source_dirs_prefers_aac_over_mp3(tmp_path):
    staging = tmp_path / "req_152"
    mp3 = staging / "mp3"
    aac = staging / "AAC"
    mp3.mkdir(parents=True)
    aac.mkdir(parents=True)
    (mp3 / "Tape1.mp3").write_bytes(b"a")
    (mp3 / "Tape2.mp3").write_bytes(b"b")
    (aac / "Tape1.m4a").write_bytes(b"c")
    (aac / "Tape2.m4a").write_bytes(b"d")
    assert m4b_source_dirs(staging) == [aac]


def test_m4b_source_dirs_multi_book_parents(tmp_path):
    staging = tmp_path / "req_pack"
    b1 = staging / "Book One"
    b2 = staging / "Book Two"
    b1.mkdir(parents=True)
    b2.mkdir(parents=True)
    (b1 / "01.mp3").write_bytes(b"a")
    (b1 / "02.mp3").write_bytes(b"b")
    (b2 / "01.mp3").write_bytes(b"c")
    (b2 / "02.mp3").write_bytes(b"d")
    assert m4b_source_dirs(staging) == [b1, b2]


def test_clean_catalog_title_strips_pack_noise():
    from app.services.forge_pipeline import clean_search_title

    assert "Harry Potter" in clean_catalog_title(
        "Harry Potter, Complete Series, Chapterized (Full-Cast Edition)"
    )
    assert "Full-Cast" not in clean_catalog_title(
        "Harry Potter, Complete Series, Chapterized (Full-Cast Edition)"
    )
    assert clean_catalog_title(
        "Shadows of Self (Mistborn, #5) graphic audio"
    ) == "Shadows of Self"
    assert clean_catalog_title(
        "req_15_Mistborn 4 - The Alloy of Law"
    ) == "The Alloy of Law"
    assert clean_catalog_title(
        "The Alloy of Law (req_15)"
    ) == "The Alloy of Law"
    assert clean_search_title("The Well of Ascension") == "Well of Ascension"
    assert clean_search_title(
        "Mistborn 6 - The Bands of Mourning graphic audio"
    ) == "Bands of Mourning"


def test_seed_metadata_hints_single_book(tmp_path):
    staging = tmp_path / "req_1_Book"
    staging.mkdir()
    (staging / "Book.m4b").write_bytes(b"x")
    seed_staging_metadata_hints(staging, title="The Gunslinger (The Dark Tower I)", author="Stephen King")
    meta = (staging / "metadata.json").read_text(encoding="utf-8")
    assert "The Gunslinger" in meta
    assert "Stephen King" in meta


def test_seed_metadata_hints_skips_series_packs(tmp_path):
    staging = tmp_path / "req_1_Pack"
    audio = staging / "Audio" / "Book1"
    audio.mkdir(parents=True)
    (audio / "01.opus").write_bytes(b"a")
    other = staging / "Audio" / "Book2"
    other.mkdir(parents=True)
    (other / "01.opus").write_bytes(b"b")
    seed_staging_metadata_hints(
        staging,
        title="Harry Potter, Complete Series, Chapterized (Full-Cast Edition)",
        author="J.K. Rowling",
    )
    assert not (audio / "metadata.json").exists()
    assert not (other / "metadata.json").exists()


def test_staging_has_applied_metadata_marker(tmp_path):
    staging = tmp_path / "req_147"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")
    assert not staging_has_applied_metadata(staging)
    (staging / "libraforge.json").write_text(
        json.dumps({"marker": {"applied": True, "score": 1.0}}),
        encoding="utf-8",
    )
    assert staging_has_applied_metadata(staging)


def test_staging_has_applied_metadata_asin_alone_is_not_enough(tmp_path):
    """Seeded metadata.json ASIN must not count as applied write evidence."""
    staging = tmp_path / "req_147"
    staging.mkdir()
    (staging / "metadata.json").write_text(
        json.dumps({"title": "The Gunslinger", "asin": "B019NNU7XE"}),
        encoding="utf-8",
    )
    assert not staging_has_applied_metadata(staging)


def test_normalize_asin_filters_sentinels():
    assert normalize_asin("b019nnu7xe") == "B019NNU7XE"
    assert normalize_asin("HAS_ASIN") == ""
    assert normalize_asin("NOREALASIN") == ""
    assert normalize_asin("") == ""
    assert normalize_asin("not-an-asin") == ""


def test_extract_asin_from_staging_prefers_book_over_metadata(tmp_path):
    staging = tmp_path / "req_ch"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")
    (staging / "libraforge.json").write_text(
        json.dumps({"book": {"asin": "B0SIDECAR1"}, "scan_cache": {"asin": "HAS_ASIN"}}),
        encoding="utf-8",
    )
    (staging / "metadata.json").write_text(
        json.dumps({"asin": "B0META0001"}),
        encoding="utf-8",
    )
    assert extract_asin_from_staging(staging) == "B0SIDECAR1"


def test_extract_asin_uses_scan_cache_when_book_asin_absent(tmp_path):
    """LibraForge complete-metadata ASIN lives in scan_cache (embedded-tag cache)."""
    staging = tmp_path / "req_ch"
    staging.mkdir()
    (staging / "book.m4b").write_bytes(b"x")
    (staging / "libraforge.json").write_text(
        json.dumps({"scan_cache": {"asin": "B0SCANCACH"}}),
        encoding="utf-8",
    )
    assert extract_asin_from_staging(staging) == "B0SCANCACH"


def test_extract_asin_from_filename_token(tmp_path):
    staging = tmp_path / "req_ch"
    staging.mkdir()
    (staging / "Book Title [B0FILENAME].m4b").write_bytes(b"x")
    assert extract_asin_from_staging(staging) == "B0FILENAME"


def test_extract_asin_falls_back_to_metadata_json(tmp_path):
    staging = tmp_path / "req_ch"
    staging.mkdir()
    (staging / "libraforge.json").write_text(
        json.dumps({"scan_cache": {"asin": "HAS_ASIN"}}),
        encoding="utf-8",
    )
    (staging / "metadata.json").write_text(
        json.dumps({"asin": "B0META0001"}),
        encoding="utf-8",
    )
    assert extract_asin_from_staging(staging) == "B0META0001"


def test_primary_audio_for_chaptering_prefers_m4b(tmp_path):
    staging = tmp_path / "req_ch"
    staging.mkdir()
    (staging / "part.mp3").write_bytes(b"a" * 100)
    (staging / "Book.m4b").write_bytes(b"b" * 50)
    assert primary_audio_for_chaptering(staging).name == "Book.m4b"


def test_primary_audio_for_chaptering_requires_m4b(tmp_path):
    """Multipart mp3-only staging must not be sent to Chapter Forge."""
    staging = tmp_path / "req_ch"
    staging.mkdir()
    (staging / "1.mp3").write_bytes(b"a" * 100)
    (staging / "2.mp3").write_bytes(b"b" * 200)
    assert primary_audio_for_chaptering(staging) is None


def test_resolve_resume_from_wizard_hints(tmp_path):
    staging = tmp_path / "req_resume"
    staging.mkdir()
    (staging / "1.mp3").write_bytes(b"a")
    (staging / "2.mp3").write_bytes(b"b")
    assert resolve_resume_from(staging) == "m4b"
    assert resolve_resume_from(staging, m4b_done=True) == "chapters"
    assert resolve_resume_from(staging, chapters_done=True) == "folder"
    assert resolve_resume_from(staging, resume_from="folder") == "folder"
    state = detect_pipeline_state(staging)
    assert state["needs_m4b"] is True
    assert state["suggested_resume"] == "m4b"


def test_detect_pipeline_state_single_m4b(tmp_path):
    staging = tmp_path / "req_m4b"
    staging.mkdir()
    (staging / "Book.m4b").write_bytes(b"x" * 20)
    (staging / "metadata.json").write_text(
        json.dumps({"asin": "B0TESTASI1", "title": "Book"}),
        encoding="utf-8",
    )
    state = detect_pipeline_state(staging)
    assert state["needs_m4b"] is False
    assert state["has_m4b"] is True
    assert state["asin"] == "B0TESTASI1"
    assert state["suggested_resume"] == "chapters"
    assert resolve_resume_from(staging) == "chapters"


def test_resolve_staging_dir_docker_style(tmp_path, monkeypatch):
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    staging = tmp_path / ".unorganized" / "req_9_Timeline"
    staging.mkdir(parents=True)
    (staging / "a.mp3").write_bytes(b"x")
    resolved = resolve_staging_dir("/audiobooks/.unorganized/req_9_Timeline")
    assert resolved == staging.resolve()
    # Legacy DB paths still resolve after rename / rewrite.
    assert resolve_staging_dir("/audiobooks/_unorganized/req_9_Timeline") == staging.resolve()


def test_resolve_staging_dir_rejects_outside_unorganized(tmp_path, monkeypatch):
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    outside = tmp_path / "Michael Crichton" / "Timeline"
    outside.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        resolve_staging_dir(str(outside))


def test_delete_request_staging_tree_docker_path_and_orphan(tmp_path, monkeypatch):
    """Reject cleanup must resolve Docker-style paths and wipe req_{id}_* leftovers."""
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    unorg = tmp_path / ".unorganized"
    primary = unorg / "req_9_Timeline"
    orphan = unorg / "req_9_OrphanLeftover"
    other = unorg / "req_10_Keep"
    primary.mkdir(parents=True)
    orphan.mkdir(parents=True)
    other.mkdir(parents=True)
    (primary / "a.mp3").write_bytes(b"x")
    (orphan / "b.mp3").write_bytes(b"y")
    (other / "c.mp3").write_bytes(b"z")
    # Library path outside staging must never be touched
    library_book = tmp_path / "Author" / "Book"
    library_book.mkdir(parents=True)
    (library_book / "keep.m4b").write_bytes(b"keep")

    deleted = delete_request_staging_tree(9, "/audiobooks/.unorganized/req_9_Timeline")
    assert primary.resolve() in {p.resolve() for p in deleted}
    assert not primary.exists()
    assert not orphan.exists()
    assert other.exists()
    assert (library_book / "keep.m4b").exists()


def test_safe_path_under_staging_blocks_traversal(tmp_path):
    staging = tmp_path / ".unorganized" / "req_1"
    staging.mkdir(parents=True)
    (staging / "keep.mp3").write_bytes(b"x")
    with pytest.raises(ValueError):
        safe_path_under_staging(staging, "../secret")
    with pytest.raises(ValueError):
        safe_path_under_staging(staging, "/etc/passwd")
    ok = safe_path_under_staging(staging, "keep.mp3")
    assert ok.name == "keep.mp3"


def test_build_staging_tree_and_delete(tmp_path):
    staging = tmp_path / "req_12_Timeline"
    sub = staging / "Audio"
    sub.mkdir(parents=True)
    (sub / "Timeline.mp3").write_bytes(b"12345")
    (sub / "Timeline.m4a").write_bytes(b"xx")
    (staging / "metadata.json").write_text("{}", encoding="utf-8")

    tree = build_staging_tree(staging)
    assert tree["root_name"] == "req_12_Timeline"
    assert tree["entry_count"] >= 3
    names = {e["name"] for e in tree["entries"]}
    assert "Audio" in names or "metadata.json" in names

    delete_staging_entry(staging, "Audio/Timeline.m4a")
    assert not (sub / "Timeline.m4a").exists()
    assert (sub / "Timeline.mp3").exists()

    # Recursive folder delete (non-empty) is allowed; staging root stays.
    with pytest.raises(ValueError, match="Path is required"):
        delete_staging_entry(staging, ".")
    delete_staging_entry(staging, "Audio")
    assert not sub.exists()
    assert staging.exists()
    assert (staging / "metadata.json").exists()


def test_cover_url_from_staging(tmp_path):
    staging = tmp_path / "req_1"
    staging.mkdir()
    (staging / "metadata.json").write_text(
        json.dumps({"title": "Timeline", "cover_url": "https://images.example/cover.jpg"}),
        encoding="utf-8",
    )
    assert cover_url_from_staging(staging) == "https://images.example/cover.jpg"


def test_cover_url_from_staging_nested_marker_audible(tmp_path):
    """Manual Review stores cover under marker.audible / sidecar.book, not top-level."""
    staging = tmp_path / "req_2"
    staging.mkdir()
    (staging / "metadata.json").write_text(
        json.dumps({"title": "Timeline", "asin": "B00TEST"}),
        encoding="utf-8",
    )
    (staging / "libraforge.json").write_text(
        json.dumps(
            {
                "marker": {
                    "audible": {
                        "title": "Timeline",
                        "cover_url": "https://images.example/nested-cover.jpg",
                    }
                },
                "sidecar": {
                    "book": {
                        "title": "Timeline",
                        "cover_url": "https://images.example/sidecar-cover.jpg",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert cover_url_from_staging(staging) == "https://images.example/nested-cover.jpg"


def test_extract_chapters_from_audible_chapters_run_report():
    """LibraForge nests chapters under stats.chaptering_result (stats.chapters is a count)."""
    report = {
        "status": "completed",
        "stats": {
            "backend": "audible-chapters",
            "asin": "B00TESTASIN",
            "chapters": 3,
            "chaptering_result": {
                "asin": "B00TESTASIN",
                "backend": "audible-chapters",
                "chapters": [
                    {"id": 1, "title": "Opening Credits", "start": 0.0, "end": 12.0},
                    {"id": 2, "title": "Chapter 1", "start": 12.0, "end": 600.0},
                    {"id": 3, "title": "Chapter 2", "start": 600.0, "end": 1200.0},
                ],
            },
        },
    }
    rows = _extract_chapters_from_report(report)
    assert len(rows) == 3
    assert rows[0]["title"] == "Opening Credits"
    assert rows[1]["start"] == 12.0


def test_extract_chapters_from_chaptering_load_payload():
    loaded = {
        "asin": "B00TESTASIN",
        "result": {
            "chapters": [
                {"title": "Intro", "start": 0},
                {"title": "Part One", "start": 30.5},
            ]
        },
    }
    rows = _extract_chapters_from_report(loaded)
    assert [r["title"] for r in rows] == ["Intro", "Part One"]
    assert rows[1]["start"] == 30.5


def test_chapter_preview_persist_roundtrip(tmp_path):
    staging = tmp_path / "req_cf"
    staging.mkdir()
    write_chapter_preview(
        staging,
        {
            "asin": "B00TESTASIN",
            "chapters": [{"index": 0, "title": "Ch 1", "start": 0.0}],
            "chapter_count": 1,
            "current_chapters": [],
            "current_chapter_count": 0,
            "status_detail": "Preview ASIN B00TESTASIN: 1 chapters",
        },
    )
    loaded = read_chapter_preview(staging)
    assert loaded is not None
    assert loaded["asin"] == "B00TESTASIN"
    assert loaded["chapter_count"] == 1
    assert loaded.get("updated_at")
    state = detect_pipeline_state(staging)
    assert state["chapter_preview"]["asin"] == "B00TESTASIN"


def test_remove_source_audio_after_m4b(tmp_path):
    staging = tmp_path / "req_1"
    staging.mkdir()
    (staging / "Timeline.mp3").write_bytes(b"a")
    (staging / "Timeline.m4a").write_bytes(b"b")
    (staging / "Timeline.m4b").write_bytes(b"c")
    removed = _remove_source_audio_after_m4b(staging)
    assert removed == 2
    assert (staging / "Timeline.m4b").exists()
    assert not (staging / "Timeline.mp3").exists()
    assert not (staging / "Timeline.m4a").exists()


def test_remove_source_audio_keeps_sidecars_and_prunes_empty_format_dirs(tmp_path):
    staging = tmp_path / "req_2"
    mp3 = staging / "mp3"
    aac = staging / "AAC"
    mp3.mkdir(parents=True)
    aac.mkdir(parents=True)
    (mp3 / "01.mp3").write_bytes(b"mp3")
    (aac / "01.m4a").write_bytes(b"m4a")
    (aac / "Book.m4b").write_bytes(b"m4b")
    (aac / "cover.jpg").write_bytes(b"jpg")
    (aac / "metadata.json").write_text("{}", encoding="utf-8")
    (staging / "libraforge.json").write_text("{}", encoding="utf-8")

    removed = _remove_source_audio_after_m4b(staging)
    assert removed >= 2
    assert (aac / "Book.m4b").exists()
    assert (aac / "cover.jpg").exists()
    assert (aac / "metadata.json").exists()
    assert (staging / "libraforge.json").exists()
    assert not (mp3 / "01.mp3").exists()
    assert not (aac / "01.m4a").exists()
    assert not mp3.exists()  # empty unused format tree pruned


def test_remove_source_audio_without_m4b_is_noop(tmp_path):
    """Soft-fail safety: never delete sources unless a .m4b is present."""
    staging = tmp_path / "req_softfail"
    staging.mkdir()
    (staging / "01.mp3").write_bytes(b"a")
    (staging / "02.mp3").write_bytes(b"b")
    assert _remove_source_audio_after_m4b(staging) == 0
    assert (staging / "01.mp3").exists()
    assert (staging / "02.mp3").exists()


def test_cleanup_forge_temps_removes_tmpfiles_not_sources(tmp_path):
    staging = tmp_path / "req_3"
    tmp = staging / "Book-tmpfiles"
    tmp.mkdir(parents=True)
    (tmp / "chunk.bin").write_bytes(b"x")
    (staging / "Book.m4b").write_bytes(b"m4b")
    (staging / "Book.m4b.part").write_bytes(b"part")
    (staging / "01.mp3").write_bytes(b"src")

    removed = _cleanup_forge_temps(staging)
    assert removed >= 2
    assert not tmp.exists()
    assert not (staging / "Book.m4b.part").exists()
    assert (staging / "Book.m4b").exists()
    assert (staging / "01.mp3").exists()


def test_cleanup_staging_after_folder_forge_wipes_when_no_audio(tmp_path, monkeypatch):
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    staging = tmp_path / ".unorganized" / "req_4_Book"
    (staging / "mp3").mkdir(parents=True)
    (staging / "cover.jpg").write_bytes(b"jpg")
    (staging / "metadata.json").write_text("{}", encoding="utf-8")

    assert _cleanup_staging_after_folder_forge(staging) is True
    assert not staging.exists()


def test_cleanup_staging_after_folder_forge_keeps_leftover_audio(tmp_path, monkeypatch):
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    staging = tmp_path / ".unorganized" / "req_5_Book"
    staging.mkdir(parents=True)
    (staging / "leftover.mp3").write_bytes(b"still here")

    assert _cleanup_staging_after_folder_forge(staging) is False
    assert (staging / "leftover.mp3").exists()


def test_cleanup_staging_after_folder_forge_force_wipes_leftover_audio(tmp_path, monkeypatch):
    """After Folder Forge moves, leftover samples must not linger in .unorganized."""
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    staging = tmp_path / ".unorganized" / "req_6_Book"
    staging.mkdir(parents=True)
    (staging / "sample.mp3").write_bytes(b"sample")
    (staging / "cover.jpg").write_bytes(b"jpg")
    library = tmp_path / "Author" / "Book"
    library.mkdir(parents=True)
    (library / "Book.m4b").write_bytes(b"m4b")

    assert _cleanup_staging_after_folder_forge(staging, force=True) is True
    assert not staging.exists()
    assert (library / "Book.m4b").exists()


def test_cleanup_staging_refuses_outside_unorganized(tmp_path, monkeypatch):
    from app.services import forge_pipeline

    monkeypatch.setattr(forge_pipeline.settings, "audiobook_dir", str(tmp_path))
    library = tmp_path / "Author" / "Book"
    library.mkdir(parents=True)
    (library / "Book.m4b").write_bytes(b"m4b")

    assert _cleanup_staging_after_folder_forge(library, force=True) is False
    assert (library / "Book.m4b").exists()
