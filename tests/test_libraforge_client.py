"""Unit tests for LibraForge client helpers (no live network)."""

from __future__ import annotations

import json

from app.services.libraforge import (
    metadata_auto_applied,
    metadata_matched_without_apply,
    organizer_moved_files,
    quarantine_reason_from_report,
    run_failed,
)
from app.services.forge_pipeline import (
    audiobook_staging_dir,
    clean_catalog_title,
    needs_m4b_conversion,
    seed_staging_metadata_hints,
    staging_has_applied_metadata,
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
    assert path.parent.name == "_unorganized"
    assert path.name.startswith("req_42_")


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
