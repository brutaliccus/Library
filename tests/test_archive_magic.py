"""Archive magic sniff -> misnamed RAR/ZIP must extract before match / Manual Review."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.downloader import (
    _is_archive,
    archive_magic_kind,
    extract_archives_in_dir,
)
from app.services.quick_review import _unwritable_media_message


def test_misnamed_m4b_rar_is_archive(tmp_path: Path):
    fake = tmp_path / "Honeybites Honeyblood, Book 2.m4b"
    fake.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 32)
    assert archive_magic_kind(fake) == "rar"
    assert _is_archive(fake)


def test_real_looking_mp4_header_is_not_archive(tmp_path: Path):
    # Typical ftyp box prefix for m4b/mp4 -- must not be treated as zip/rar.
    real = tmp_path / "book.m4b"
    real.write_bytes(b"\x00\x00\x00\x20ftypM4B " + b"\x00" * 16)
    assert archive_magic_kind(real) is None
    assert not _is_archive(real)


def test_zip_extension_is_still_archive(tmp_path: Path):
    z = tmp_path / "pack.zip"
    z.write_bytes(b"not-magic-but-extension")
    assert _is_archive(z)


def test_cbz_not_sniffed_as_archive(tmp_path: Path):
    """Comic archives keep their extension; magic sniff is audio-suffix only."""
    cbz = tmp_path / "issue.cbz"
    cbz.write_bytes(b"PK\x03\x04" + b"\x00" * 16)
    assert not _is_archive(cbz)


def test_extract_archives_in_dir_pulls_misnamed_rar(tmp_path: Path):
    staging = tmp_path / "req_35"
    staging.mkdir()
    fake = staging / "Book.m4b"
    fake.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 8)

    async def _fake_extract(archive_path: Path) -> bool:
        # Simulate 7z: drop a real audio file and remove the archive.
        (archive_path.parent / "chapter01.mp3").write_bytes(b"ID3")
        archive_path.unlink(missing_ok=True)
        return True

    with patch(
        "app.services.downloader.extract_archive",
        new=AsyncMock(side_effect=_fake_extract),
    ):
        names = asyncio.run(extract_archives_in_dir(staging))

    assert names == ["Book.m4b"]
    assert not fake.exists()
    assert (staging / "chapter01.mp3").is_file()


def test_unwritable_media_message_maps_moov_error():
    tip = _unwritable_media_message(
        "LibraForge POST /api/manual-review/apply -> HTTP 500: "
        "ffmpeg failed ... moov atom not found"
    )
    assert tip is not None
    assert "RAR/ZIP" in tip
    assert _unwritable_media_message("timeout talking to LibraForge") is None


def test_forge_pipeline_extracts_before_metadata_match(tmp_path: Path):
    """run_forge_after_download must unpack archive-magic audio before LibraForge match."""
    from app.services.forge_pipeline import run_forge_after_download

    staging = tmp_path / "req_99_Honeybites"
    staging.mkdir()
    fake = staging / "Honeybites Honeyblood, Book 2.m4b"
    fake.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 32)

    order: list[str] = []

    async def _track_extract(root: Path, **_kwargs):
        order.append("extract")
        assert root == staging
        assert any(
            p.is_file() and archive_magic_kind(p) == "rar" for p in root.rglob("*")
        )
        return [fake.name]

    async def _track_apply(*_args, **_kwargs):
        order.append("match")
        return False

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    session.commit = AsyncMock()

    pipeline = MagicMock()
    pipeline._is_cancelled = AsyncMock(return_value=False)
    pipeline._update_status = AsyncMock()
    pipeline._report_progress = AsyncMock()

    async def _run():
        with (
            patch("app.services.forge_pipeline.async_session", return_value=session),
            patch("app.services.forge_pipeline._pipeline", return_value=pipeline),
            patch(
                "app.services.forge_pipeline._persist_staging",
                new=AsyncMock(),
            ),
            patch(
                "app.services.forge_pipeline._ensure_staging_archives_extracted_before_match",
                new=AsyncMock(side_effect=_track_extract),
            ),
            patch(
                "app.services.forge_pipeline._apply_metadata_forge",
                new=AsyncMock(side_effect=_track_apply),
            ) as apply_mock,
            patch(
                "app.services.llm_assist.maybe_handle_multi_book",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.llm_assist.maybe_auto_prune_or_suggest",
                new=AsyncMock(),
            ),
            patch(
                "app.services.forge_pipeline.seed_staging_metadata_hints",
            ),
        ):
            await run_forge_after_download(
                99,
                staging=staging,
                user_id=1,
                title="Honeybites Honeyblood, Book 2",
                author="Author",
                resume_from="metadata",
            )
            return apply_mock

    apply_mock = asyncio.run(_run())
    assert order == ["extract", "match"]
    apply_mock.assert_awaited_once()


def test_ensure_staging_archives_extracted_before_match_uses_downloader(
    tmp_path: Path,
):
    from app.services.forge_pipeline import (
        _ensure_staging_archives_extracted_before_match,
    )

    staging = tmp_path / "req_1"
    staging.mkdir()
    (staging / "Book.m4b").write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 8)

    with patch(
        "app.services.downloader.extract_archives_in_dir",
        new=AsyncMock(return_value=["Book.m4b"]),
    ) as extract:
        names = asyncio.run(
            _ensure_staging_archives_extracted_before_match(staging, request_id=1)
        )

    assert names == ["Book.m4b"]
    extract.assert_awaited_once_with(staging)