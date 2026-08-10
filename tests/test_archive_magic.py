"""Archive magic sniff — misnamed RAR/ZIP must extract before Manual Review apply."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.downloader import (
    _is_archive,
    archive_magic_kind,
    extract_archives_in_dir,
)
from app.services.quick_review import _unwritable_media_message


def test_misnamed_m4b_rar_is_archive(tmp_path: Path):
    fake = tmp_path / "Honeybites Honeybloods, Book 2.m4b"
    fake.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 32)
    assert archive_magic_kind(fake) == "rar"
    assert _is_archive(fake)


def test_real_looking_mp4_header_is_not_archive(tmp_path: Path):
    # Typical ftyp box prefix for m4b/mp4 — must not be treated as zip/rar.
    real = tmp_path / "book.m4b"
    real.write_bytes(b"\x00\x00\x00\x20ftypM4B " + b"\x00" * 16)
    assert archive_magic_kind(real) is None
    assert not _is_archive(real)


def test_zip_extension_still_archive(tmp_path: Path):
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
