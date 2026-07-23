"""Unit tests for LibraForge client helpers (no live network)."""

from __future__ import annotations

from app.services.libraforge import (
    metadata_auto_applied,
    quarantine_reason_from_report,
    run_failed,
)
from app.services.forge_pipeline import audiobook_staging_dir, needs_m4b_conversion


def test_metadata_auto_applied_mode_full():
    assert metadata_auto_applied({"files_by_category": {"mode:full": [{"path": "/x"}]}})


def test_metadata_auto_applied_mode_breakdown():
    assert metadata_auto_applied({"stats": {"mode_breakdown": {"full": 1}}})


def test_metadata_not_applied_when_empty():
    assert not metadata_auto_applied({"files_by_category": {"mode:none": [{"path": "/x"}]}})


def test_run_failed_on_error_status():
    assert run_failed({"status": "failed", "error": "boom"})
    assert not run_failed({"status": "completed", "returncode": 0})


def test_quarantine_reason_from_manual_items():
    reason = quarantine_reason_from_report(
        {"manual_review_items": [{"reason": "low score", "path": "/a"}]}
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
