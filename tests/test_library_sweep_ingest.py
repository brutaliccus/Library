"""Library Sweep + owned upload backend smoke tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import DownloadRequest, LibrarySweepJob
from app.services import library_ingest


def test_download_request_has_ingest_columns():
    for name in ("source", "abs_item_id", "ingest_fingerprint"):
        assert hasattr(DownloadRequest, name)
        assert name in DownloadRequest.__table__.c


def test_library_sweep_job_model():
    assert LibrarySweepJob.__tablename__ == "library_sweep_jobs"
    for name in (
        "status",
        "total",
        "scanned",
        "auto_applied",
        "needs_review",
        "failed",
        "m4b_queued",
        "review_cursor_request_id",
        "error",
        "started_by_user_id",
    ):
        assert hasattr(LibrarySweepJob, name)


def test_synthetic_magnets_and_fingerprint():
    assert library_ingest.sweep_magnet("abc123") == "sweep:abs:abc123"
    assert library_ingest.sweep_fingerprint("abc123") == "abs:abc123"
    mag = library_ingest.upload_magnet("deadbeef")
    assert mag == "upload:deadbeef"
    assert library_ingest.upload_magnet().startswith("upload:")


def test_stage_tree_copy_and_hardlink(tmp_path: Path):
    src = tmp_path / "lib" / "Book"
    src.mkdir(parents=True)
    audio = src / "chapter.mp3"
    audio.write_bytes(b"ID3fake")
    dest = tmp_path / "staging" / "req_1_Book"
    method = library_ingest.stage_tree_from_library(src, dest, prefer_hardlink=True)
    assert method in ("hardlink", "copy")
    assert (dest / "chapter.mp3").is_file()
    assert (dest / "chapter.mp3").read_bytes() == b"ID3fake"


def test_stage_uploaded_files(tmp_path: Path):
    dest = tmp_path / "up"
    n = library_ingest.stage_uploaded_files(
        dest,
        [("My Book.m4b", b"audio"), ("cover.jpg", b"img")],
    )
    assert n == 2
    assert (dest / "My Book.m4b").read_bytes() == b"audio"


def test_is_audio_filename():
    assert library_ingest.is_audio_filename("x.m4b")
    assert library_ingest.is_audio_filename("x.MP3")
    assert not library_ingest.is_audio_filename("x.jpg")


def test_admin_sweep_routes_registered():
    from app.routers import admin

    paths = {getattr(r, "path", "") for r in admin.router.routes}
    assert any("library-sweep/start" in p for p in paths)
    assert any("library-sweep/pause" in p for p in paths)
    assert any("library-sweep/cancel" in p for p in paths)
    assert any("library-sweep/status" in p for p in paths)
    assert any("library-sweep/needs-review" in p for p in paths)
    assert any("library-sweep/review-cursor" in p for p in paths)


def test_owned_upload_routes_registered():
    from app.routers import library

    paths = {getattr(r, "path", "") for r in library.router.routes}
    assert any(p.endswith("/owned-uploads") for p in paths)
    assert any("owned-uploads/allowed" in p for p in paths)


def test_allow_user_setting_in_registry():
    from app.services import instance_settings

    keys = {d.key for d in instance_settings.REGISTRY}
    assert "allow_user_audiobook_upload" in keys


def test_user_may_upload_admin_always(monkeypatch):
    import asyncio

    async def _false(_key, default=False):
        return False

    monkeypatch.setattr(
        "app.services.instance_settings.get_effective_bool",
        _false,
    )
    assert asyncio.run(library_ingest.user_may_upload_owned("admin")) is True
    assert asyncio.run(library_ingest.user_may_upload_owned("user")) is False


def test_user_may_upload_when_setting_on(monkeypatch):
    import asyncio

    async def _true(_key, default=False):
        return True

    monkeypatch.setattr(
        "app.services.instance_settings.get_effective_bool",
        _true,
    )
    assert asyncio.run(library_ingest.user_may_upload_owned("user")) is True


def test_migration_0014_exists():
    root = Path(__file__).resolve().parents[1]
    mig = root / "migrations" / "versions" / "0014_library_sweep_ingest.py"
    assert mig.is_file()
    text = mig.read_text(encoding="utf-8")
    assert 'revision = "0014"' in text
    assert 'down_revision = "0013"' in text
    assert "library_sweep_jobs" in text
