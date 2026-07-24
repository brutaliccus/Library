"""Unit tests for LibraForge client helpers (no live network)."""

from __future__ import annotations

import json

import pytest

from app.services.libraforge import (
    metadata_auto_applied,
    metadata_matched_without_apply,
    organizer_moved_files,
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
    extract_asin_from_staging,
    m4b_source_dirs,
    needs_m4b_conversion,
    normalize_asin,
    primary_audio_for_chaptering,
    resolve_staging_dir,
    safe_path_under_staging,
    seed_staging_metadata_hints,
    staging_has_applied_metadata,
    _cleanup_forge_temps,
    _cleanup_staging_after_folder_forge,
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


def test_needs_m4b_single_m4b(tmp_path):
    book = tmp_path / "book"
    book.mkdir()
    (book / "Title.m4b").write_bytes(b"x")
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
    assert "Harry Potter" in clean_catalog_title(
        "Harry Potter, Complete Series, Chapterized (Full-Cast Edition)"
    )
    assert "Full-Cast" not in clean_catalog_title(
        "Harry Potter, Complete Series, Chapterized (Full-Cast Edition)"
    )


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


def test_staging_has_applied_metadata_asin(tmp_path):
    staging = tmp_path / "req_147"
    staging.mkdir()
    (staging / "metadata.json").write_text(
        json.dumps({"title": "The Gunslinger", "asin": "B019NNU7XE"}),
        encoding="utf-8",
    )
    assert staging_has_applied_metadata(staging)


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


def test_cleanup_staging_after_folder_forge_wipes_when_no_audio(tmp_path):
    staging = tmp_path / "req_4_Book"
    (staging / "mp3").mkdir(parents=True)
    (staging / "cover.jpg").write_bytes(b"jpg")
    (staging / "metadata.json").write_text("{}", encoding="utf-8")

    assert _cleanup_staging_after_folder_forge(staging) is True
    assert not staging.exists()


def test_cleanup_staging_after_folder_forge_keeps_leftover_audio(tmp_path):
    staging = tmp_path / "req_5_Book"
    staging.mkdir()
    (staging / "leftover.mp3").write_bytes(b"still here")

    assert _cleanup_staging_after_folder_forge(staging) is False
    assert (staging / "leftover.mp3").exists()
