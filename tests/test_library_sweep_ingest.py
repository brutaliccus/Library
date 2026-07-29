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


def test_staging_audio_hardlinked_helper(tmp_path: Path):
    from app.services.forge_pipeline import _staging_audio_hardlinked

    staging = tmp_path / "staging"
    staging.mkdir()
    audio = staging / "book.m4b"
    audio.write_bytes(b"m4b")
    assert not _staging_audio_hardlinked(staging)
    # Second hardlink name → nlink >= 2
    other = tmp_path / "library_copy.m4b"
    try:
        other.hardlink_to(audio)
    except OSError:
        pytest.skip("hardlinks unsupported on this filesystem")
    assert _staging_audio_hardlinked(staging)


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
    assert any("library-sweep/skip" in p for p in paths)
    assert any("library-sweep/unprocessed" in p for p in paths)
    assert any("library-sweep/processed" in p for p in paths)
    assert any("library-sweep/reprocess" in p for p in paths)
    assert any("library-sweep/dismiss" in p for p in paths)


def test_synthetic_magnet_helpers():
    assert library_ingest.is_synthetic_magnet("sweep:abs:abc")
    assert library_ingest.is_synthetic_magnet("upload:deadbeef")
    assert not library_ingest.is_synthetic_magnet("magnet:?xt=urn:btih:abc")
    assert library_ingest.is_local_ingest_source("sweep")
    assert library_ingest.is_local_ingest_source("upload")
    assert not library_ingest.is_local_ingest_source("request")


def test_owned_upload_routes_registered():
    from app.routers import library

    paths = {getattr(r, "path", "") for r in library.router.routes}
    assert any(p.endswith("/owned-uploads") for p in paths)
    assert any("owned-uploads/allowed" in p for p in paths)


def test_allow_user_setting_in_registry():
    from app.services import instance_settings

    keys = {d.key for d in instance_settings.REGISTRY}
    assert "allow_user_audiobook_upload" in keys


def test_sweep_pipeline_settings_in_registry():
    from app.services import instance_settings

    keys = {d.key for d in instance_settings.REGISTRY}
    assert "config.libraforge_naming_template" in keys
    assert "config.libraforge_metadata_provider" in keys
    assert "config.library_sweep_abs_scan_every" in keys


def test_sweep_dismissed_status_constant():
    assert library_ingest.STATUS_SWEEP_DISMISSED == "sweep_dismissed"
    assert "sweep_dismissed" in library_ingest._SWEEP_WALK_SKIP_STATUSES
    assert "sweep_dismissed" not in library_ingest._UNPROCESSED_STATUSES


def test_metadata_provider_chain_order():
    from app.services import libraforge

    assert libraforge.metadata_provider_chain("audible") == [
        "audible",
        "graphicaudio",
        "soundbooththeater",
    ]
    assert libraforge.metadata_provider_chain("graphicaudio") == [
        "graphicaudio",
        "soundbooththeater",
    ]
    assert libraforge.metadata_provider_chain("soundbooththeater") == [
        "soundbooththeater",
        "graphicaudio",
    ]


def test_abs_scan_every_helper(monkeypatch):
    from app.services import library_sweep

    monkeypatch.setattr(library_sweep.settings, "library_sweep_abs_scan_every", 25)
    assert library_sweep._abs_scan_every() == 25
    monkeypatch.setattr(library_sweep.settings, "library_sweep_abs_scan_every", 0)
    assert library_sweep._abs_scan_every() == 1
    monkeypatch.setattr(library_sweep.settings, "library_sweep_abs_scan_every", "nope")
    assert library_sweep._abs_scan_every() == 25


def test_abs_item_preview_and_up_next_helpers():
    from app.services import library_sweep

    item = {
        "id": "abs-1",
        "media": {"metadata": {"title": "Mistborn", "authorName": "Sanderson"}},
    }
    preview = library_sweep._abs_item_preview(item)
    assert preview["title"] == "Mistborn"
    assert preview["author"] == "Sanderson"
    assert preview["abs_item_id"] == "abs-1"
    assert "abs-1" in (preview["cover_url"] or "")

    library_sweep._set_up_next(preview)
    assert library_sweep._up_next and library_sweep._up_next["title"] == "Mistborn"
    library_sweep._set_up_next(None)
    assert library_sweep._up_next is None


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
