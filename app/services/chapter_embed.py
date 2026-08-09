"""Embed chapter markers into MP4-family audiobooks (Library-owned, no LibraForge fork).

Upstream LibraForge Chapter Forge (audible-chapters) fetches Audible chapter lists and
writes sidecar/cue JSON. Players and Audiobookshelf read markers from the .m4b itself.
This module remuxes those markers via ffmpeg stream copy (no re-encode).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MP4_CHAPTER_EXTENSIONS = frozenset({".m4b", ".m4a", ".mp4"})
AUDIO_EXTENSIONS = frozenset(
    {".m4b", ".m4a", ".mp4", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".wav"}
)


class ChapterEmbedError(RuntimeError):
    """Chapter remux failed (missing ffmpeg, bad input, ffmpeg non-zero, etc.)."""


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def can_embed_chapters(source: Path) -> bool:
    return source.is_file() and source.suffix.lower() in MP4_CHAPTER_EXTENSIONS


def _ffmetadata_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def write_ffmetadata(chapters: list[dict[str, Any]], duration: float | None = None) -> str:
    """Build an ffmetadata file that replaces chapter markers (no re-encode)."""
    lines = [";FFMETADATA1"]
    for index, chapter in enumerate(chapters):
        start = max(0.0, float(chapter.get("start") or 0.0))
        end_raw = chapter.get("end")
        if end_raw is None and index + 1 < len(chapters):
            end_raw = chapters[index + 1].get("start")
        if end_raw is None:
            end_raw = duration if duration and duration > start else start + 1.0
        end = max(start, float(end_raw))
        start_ms = int(round(start * 1000))
        end_ms = int(round(end * 1000))
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        title = _ffmetadata_escape(
            str(chapter.get("title") or "").strip() or f"Chapter {index + 1}"
        )
        lines.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start_ms}",
                f"END={end_ms}",
                f"title={title}",
            ]
        )
    return "\n".join(lines) + "\n"


def _chapter_start(ch: dict[str, Any]) -> float:
    start = ch.get("start")
    if start is None:
        start = ch.get("start_sec") or ch.get("startSeconds")
    if start is None and ch.get("start_ms") is not None:
        try:
            return float(ch["start_ms"]) / 1000.0
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(start or 0)
    except (TypeError, ValueError):
        return 0.0


def _chapter_end(ch: dict[str, Any]) -> float | None:
    end = ch.get("end")
    if end is None:
        end = ch.get("end_sec") or ch.get("endSeconds")
    if end is None and ch.get("end_ms") is not None:
        try:
            return float(ch["end_ms"]) / 1000.0
        except (TypeError, ValueError):
            return None
    if end is None and ch.get("length_ms") is not None:
        try:
            return _chapter_start(ch) + float(ch["length_ms"]) / 1000.0
        except (TypeError, ValueError):
            return None
    try:
        return float(end) if end is not None else None
    except (TypeError, ValueError):
        return None


def normalize_chapters_for_embed(raw: Any) -> list[dict[str, Any]]:
    """Normalize chapter dicts with start/end/title for ffmpeg remux."""
    if not isinstance(raw, list) or not raw:
        return []
    out: list[dict[str, Any]] = []
    for i, ch in enumerate(raw):
        if not isinstance(ch, dict):
            continue
        title = str(ch.get("title") or ch.get("name") or f"Chapter {i + 1}").strip()
        row: dict[str, Any] = {
            "id": i + 1,
            "title": title or f"Chapter {i + 1}",
            "start": _chapter_start(ch),
        }
        end = _chapter_end(ch)
        if end is not None:
            row["end"] = end
        out.append(row)
    out.sort(key=lambda c: float(c.get("start") or 0))
    for i, row in enumerate(out):
        row["id"] = i + 1
        if row.get("end") is None and i + 1 < len(out):
            row["end"] = float(out[i + 1]["start"])
    return out


def chapters_from_run_report(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Pull a full chapter list (with ends when present) from a LibraForge run payload."""
    if not isinstance(report, dict):
        return []
    stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
    nested_result = report.get("result") if isinstance(report.get("result"), dict) else {}
    chaptering_result = (
        stats.get("chaptering_result")
        if isinstance(stats.get("chaptering_result"), dict)
        else {}
    )
    for raw in (
        report.get("chapters"),
        nested_result.get("chapters"),
        chaptering_result.get("chapters"),
        stats.get("chapters_list"),
    ):
        rows = normalize_chapters_for_embed(raw)
        if rows:
            return rows
    return []


def _companion_sidecar_paths(audio: Path) -> list[Path]:
    folder = audio.parent
    try:
        audio_count = sum(
            1
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        )
    except OSError:
        audio_count = 1
    paths = [folder / "libraforge.json", audio.with_name(audio.name + ".libraforge.json")]
    if audio_count > 1:
        # Prefer per-file sidecar when several audio files share a folder.
        paths = [paths[1], paths[0]]
    return paths


def chapters_from_libraforge_sidecar(audio: Path) -> list[dict[str, Any]]:
    """Best-effort read of chapters LibraForge wrote next to the .m4b."""
    for path in _companion_sidecar_paths(audio):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        chapter_forge = data.get("chapter_forge")
        if isinstance(chapter_forge, dict):
            rows = normalize_chapters_for_embed(chapter_forge.get("chapters"))
            if rows:
                return rows
        # Nested libraforge.json shape
        nested = data.get("sidecar") if isinstance(data.get("sidecar"), dict) else {}
        nested_cf = nested.get("chapter_forge") if isinstance(nested, dict) else None
        if isinstance(nested_cf, dict):
            rows = normalize_chapters_for_embed(nested_cf.get("chapters"))
            if rows:
                return rows
    return []


def duration_from_run_report(report: dict[str, Any] | None) -> float | None:
    if not isinstance(report, dict):
        return None
    stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
    chaptering_result = (
        stats.get("chaptering_result")
        if isinstance(stats.get("chaptering_result"), dict)
        else {}
    )
    for candidate in (
        stats.get("duration"),
        chaptering_result.get("duration"),
        report.get("duration"),
    ):
        try:
            val = float(candidate)
            if val > 0:
                return val
        except (TypeError, ValueError):
            continue
    return None


def _read_mp4_asin(source: Path) -> str:
    try:
        from mutagen.mp4 import MP4, MP4FreeForm  # type: ignore[import-untyped]
    except Exception:
        return ""
    keys = (
        "----:com.apple.iTunes:asin",
        "----:com.apple.iTunes:ASIN",
        "----:com.pilabor.tone:AUDIBLE_ASIN",
    )
    try:
        tags = MP4(str(source)).tags or {}
    except Exception:
        return ""
    for key in keys:
        raw = tags.get(key) or []
        if not raw:
            continue
        val = raw[0]
        if isinstance(val, (bytes, bytearray, MP4FreeForm)):
            text_v = bytes(val).decode("utf-8", errors="ignore").strip()
        else:
            text_v = str(val).strip()
        if text_v:
            return text_v
    return ""


def _write_mp4_asin(source: Path, asin: str) -> None:
    asin = (asin or "").strip()
    if not asin:
        return
    try:
        from mutagen.mp4 import MP4, MP4FreeForm  # type: ignore[import-untyped]
    except Exception:
        return
    try:
        mp4 = MP4(str(source))
        if mp4.tags is None:
            mp4.add_tags()
        payload = [MP4FreeForm(asin.encode("utf-8"))]
        mp4.tags["----:com.apple.iTunes:asin"] = payload
        mp4.tags["----:com.pilabor.tone:AUDIBLE_ASIN"] = payload
        mp4.save()
    except Exception:
        logger.debug("Could not re-apply ASIN tag after chapter remux", exc_info=True)


def embed_chapters_into_audio(
    source: Path,
    chapters: list[dict[str, Any]],
    *,
    duration: float | None = None,
    asin: str | None = None,
) -> Path:
    """Rewrite chapter markers on an MP4-family audiobook via ffmpeg stream copy."""
    chapters = normalize_chapters_for_embed(chapters)
    if not chapters:
        raise ChapterEmbedError("No chapters to embed")
    if not can_embed_chapters(source):
        raise ChapterEmbedError(
            f"Chapter embedding requires a single .m4b/.m4a/.mp4 file, got {source}"
        )
    if not ffmpeg_available():
        raise ChapterEmbedError(
            "ffmpeg not found on PATH   install ffmpeg in the Library app image/host"
        )

    preserve_asin = (asin or "").strip() or _read_mp4_asin(source)
    meta_text = write_ffmetadata(chapters, duration=duration)
    tmp_out = source.with_name(f".{source.stem}.chapters-tmp{source.suffix}")
    meta_path = source.with_name(f".{source.stem}.chapters.ffmeta")
    try:
        meta_path.write_text(meta_text, encoding="utf-8")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-i",
            str(meta_path),
            "-map",
            "0:a?",
            "-map",
            "0:v?",
            "-map_metadata",
            "0",
            "-map_chapters",
            "1",
            "-c",
            "copy",
            "-sn",
            "-dn",
            str(tmp_out),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(120, int((duration or 3600) / 2) + 120),
            check=False,
        )
        if result.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size < 1000:
            detail = (result.stderr or result.stdout or "ffmpeg failed").strip()[:800]
            raise ChapterEmbedError(f"Failed to embed chapters into {source.name}: {detail}")
        os.replace(tmp_out, source)
        if preserve_asin:
            _write_mp4_asin(source, preserve_asin)
        return source
    finally:
        for path in (tmp_out, meta_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
