"""Torrent / ABB release file lists for multi-book pack splitting.

ABB detail pages and debrid torrent_info expose per-file paths that name
books clearly even when downloads land flat in staging. These helpers
normalize that metadata and map it onto on-disk staging files by basename.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_AUDIO_EXT = {
    ".mp3", ".m4b", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".aac", ".wav",
}
_SKIP_NAME_RE = re.compile(
    r"(?:^|/)(?:\.|__MACOSX|sample|preview|promo)(?:/|$)",
    re.IGNORECASE,
)
_SIZE_TOKEN_RE = re.compile(
    r"^(?P<size>[\d.]+)\s*(?P<unit>TB|GB|MB|KB)s?$",
    re.IGNORECASE,
)


def parse_size_to_bytes(size_text: str) -> int:
    m = re.search(r"([\d.]+)\s*(TB|GB|MB|KB)s?", size_text or "", re.I)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 1)
    return int(val * mult)


def normalize_release_path(raw: str) -> str:
    path = (raw or "").strip().replace("\\", "/")
    path = re.sub(r"/+", "/", path).strip("/")
    return path


def normalize_release_files(raw: Any) -> list[dict[str, Any]]:
    """Normalize heterogeneous file rows to [{path, name, size_bytes}]."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            path = normalize_release_path(item)
            size_bytes = 0
        elif isinstance(item, dict):
            path = normalize_release_path(
                str(item.get("path") or item.get("name") or item.get("filename") or "")
            )
            try:
                size_bytes = int(
                    item.get("size_bytes") or item.get("bytes") or item.get("size") or 0
                )
            except (TypeError, ValueError):
                size_bytes = 0
            if not size_bytes and item.get("size_text"):
                size_bytes = parse_size_to_bytes(str(item.get("size_text")))
        else:
            continue
        if not path or path in seen:
            continue
        if _SKIP_NAME_RE.search(path):
            continue
        seen.add(path)
        out.append({
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "size_bytes": size_bytes,
        })
    return out


def dumps_release_files(files: list[dict[str, Any]]) -> str:
    return json.dumps(normalize_release_files(files), ensure_ascii=False)


def loads_release_files(raw: str | None) -> list[dict[str, Any]]:
    if not (raw or "").strip():
        return []
    try:
        return normalize_release_files(json.loads(raw))
    except json.JSONDecodeError:
        return []


def release_files_from_torrent_info(torrent_info: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Pull path/bytes from a debrid get_torrent_info payload."""
    if not torrent_info:
        return []
    rows: list[dict[str, Any]] = []
    for f in torrent_info.get("files") or []:
        if not isinstance(f, dict):
            continue
        path = f.get("path") or f.get("name") or f.get("filename") or ""
        if not path:
            continue
        # RD paths often start with "/"
        path = str(path).lstrip("/")
        try:
            size_bytes = int(f.get("bytes") or f.get("size") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        rows.append({"path": path, "size_bytes": size_bytes})
    return normalize_release_files(rows)


def group_release_files_by_book(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group release files into books using the top-level path segment."""
    files = normalize_release_files(files)
    buckets: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        path = f["path"]
        if "/" in path:
            key = path.split("/", 1)[0].strip()
        else:
            key = _flat_book_key(path)
        if not key:
            key = "_root"
        buckets.setdefault(key, []).append(f)

    groups: list[dict[str, Any]] = []
    for key, rows in buckets.items():
        audio = [r for r in rows if Path(r["name"]).suffix.lower() in _AUDIO_EXT]
        if not audio:
            continue
        title = _title_from_group_key(key)
        groups.append({
            "key": key,
            "title": title,
            "files": rows,
            "audio_names": [r["name"] for r in audio],
        })
    groups.sort(key=lambda g: g["title"].lower())
    return groups


def _flat_book_key(filename: str) -> str:
    stem = Path(filename).stem
    m = re.match(r"^([A-Z]{3,}\d{2})\d{0,2}P?\d*$", stem, re.I)
    if m:
        return m.group(1).upper()
    m = re.match(
        r"^(.+?)\s+(?:pt\.?\s*\d+|part\s*\d+|\(\d+\s*of\s*\d+\))\s*$",
        stem,
        re.I,
    )
    if m:
        return m.group(1).strip()
    return stem


def _title_from_group_key(key: str) -> str:
    if re.fullmatch(r"[A-Z]+\d{2}", key or "", re.I):
        return key.upper()
    title = key
    title = re.sub(r"^\d+\s+", "", title)
    title = re.sub(
        r"\s*\[(?:Graphic\s*Audio|GraphicAudio)[^\]]*\]\s*",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip(" -_")
    return title or key


def _norm_name(name: str) -> str:
    """Normalize basenames for fuzzy match (whitespace / unicode dashes)."""
    s = (name or "").lower().replace("\\", "/")
    s = s.rsplit("/", 1)[-1]
    for a, b in (
        ("‐", "-"), ("‑", "-"), ("‒", "-"),
        ("–", "-"), ("—", "-"), ("−", "-"),
        ("꞉", ":"), ("：", ":"),
    ):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s).strip()
    return s



def map_release_groups_to_staging(
    staging: Path,
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach staging-relative paths to each release group via basename match."""
    if not staging.is_dir() or not groups:
        return []
    by_name: dict[str, list[str]] = {}
    for path in staging.rglob("*"):
        if not path.is_file():
            continue
        if "-tmpfiles" in path.parts:
            continue
        if path.name.endswith(".libraforge.json"):
            continue
        rel = path.relative_to(staging).as_posix()
        by_name.setdefault(_norm_name(path.name), []).append(rel)

    mapped: list[dict[str, Any]] = []
    used: set[str] = set()
    for group in groups:
        hits: list[str] = []
        for name in group.get("audio_names") or []:
            for rel in by_name.get(_norm_name(str(name)), []):
                if rel in used:
                    continue
                hits.append(rel)
                used.add(rel)
        for row in group.get("files") or []:
            name = str(row.get("name") or "")
            if not name or Path(name).suffix.lower() in _AUDIO_EXT:
                continue
            for rel in by_name.get(_norm_name(name), []):
                if rel in used:
                    continue
                hits.append(rel)
                used.add(rel)
        if not hits:
            continue
        parents = {str(Path(h).parent.as_posix()) for h in hits if "/" in h}
        if len(parents) == 1 and next(iter(parents)) != ".":
            paths = [next(iter(parents))]
            folder_based = True
        else:
            paths = sorted(hits)
            folder_based = False
        mapped.append({
            "title": group.get("title") or "Unknown",
            "author": "",
            "paths": paths,
            "confidence": 0.9,
            "release_key": group.get("key") or "",
            "folder_based": folder_based,
        })
    return mapped


def build_split_plan_from_release_files(
    staging: Path,
    release_files: list[dict[str, Any]] | str | None,
    *,
    default_author: str = "",
) -> dict[str, Any] | None:
    """Return a BookSplitPlan-compatible dict when ≥2 books map onto staging."""
    files = normalize_release_files(release_files)
    groups = group_release_files_by_book(files)
    if len(groups) < 2:
        return None
    mapped = map_release_groups_to_staging(staging, groups)
    if len(mapped) < 2:
        return None
    books = []
    folder_based = True
    for m in mapped:
        folder_based = folder_based and bool(m.get("folder_based"))
        books.append({
            "title": m["title"],
            "author": default_author or "",
            "paths": m["paths"],
            "confidence": m["confidence"],
        })
    return {
        "books": books,
        "confidence": 0.92,
        "rationale": (
            f"Grouped {len(books)} books from AudioBookBay/debrid file list "
            "(basename match onto staging)."
        ),
        "folder_based": folder_based,
    }


def parse_file_list_text(text: str) -> list[dict[str, Any]]:
    """Parse pasted ABB-style lines: path segments + filename + size."""
    rows: list[dict[str, Any]] = []
    media_ext_re = re.compile(
        r"\.(?:mp3|m4b|m4a|flac|ogg|opus|aac|wav|epub|mobi|azw3|pdf|"
        r"jpg|jpeg|png|opf|cue|json|txt|nfo)$",
        re.IGNORECASE,
    )
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        size_bytes = 0
        if len(tokens) >= 2 and _SIZE_TOKEN_RE.match(f"{tokens[-2]} {tokens[-1]}"):
            size_bytes = parse_size_to_bytes(f"{tokens[-2]} {tokens[-1]}")
            tokens = tokens[:-2]
        elif _SIZE_TOKEN_RE.match(tokens[-1]):
            size_bytes = parse_size_to_bytes(tokens[-1])
            tokens = tokens[:-1]
        if not tokens:
            continue
        body = " ".join(tokens)
        if not media_ext_re.search(body):
            continue
        # Prefer split after GraphicAudio marker: remainder is usually the filename.
        split = re.match(
            r"^(?P<top>.+?(?:\[\s*Graphic\s*Audio[^\]]*\]|Graphic\s*Audio))\s+(?P<rest>.+)$",
            body,
            re.I,
        )
        if split and media_ext_re.search(split.group("rest")):
            top = split.group("top").strip()
            filename = split.group("rest").strip()
            path = f"{top}/{filename}"
        else:
            # Fallback: last token with extension; include leading "pt N" fragments.
            filename_idx = None
            for i in range(len(tokens) - 1, -1, -1):
                if media_ext_re.search(tokens[i]):
                    filename_idx = i
                    break
            if filename_idx is None:
                continue
            while filename_idx > 0 and re.fullmatch(
                r"(?:pt\.?|part|cd|disc|\d+)", tokens[filename_idx - 1], re.I
            ):
                filename_idx -= 1
            # If still a tiny name like "1.mp3", take a wider trailing window.
            filename = " ".join(tokens[filename_idx:])
            folders = " ".join(tokens[:filename_idx]).strip()
            if " " not in filename and " - " in body:
                # Spaced real filename collapsed to last token (e.g. Novel.m4b).
                m = re.match(r"^(?P<head>.+ - )(?P<name>.+\.[A-Za-z0-9]{2,5})$", body)
                if m and " " in m.group("name"):
                    folders = m.group("head").rstrip(" -")
                    filename = m.group("name")
            path = f"{folders}/{filename}" if folders else filename
        rows.append({"path": path, "size_bytes": size_bytes})
    return normalize_release_files(rows)

