#!/usr/bin/env python3
"""Batch-apply Audible chapters (Chapter Forge) across an audiobook library.

Inventories book folders under AUDIOBOOKS_ROOT (default /audiobooks), resolves
ASIN from libraforge.json / metadata.json, and for each book with a real ASIN
calls LibraForge ``POST /api/chaptering/runs`` (backend=audible-chapters).

Designed to run on the Pi (or any host that can reach LibraForge + the library
mount). Concurrency defaults to 1 (Audible-friendly; Chapter Forge is light).

Usage (on Pi, from Library Site or LibraForge reports dir):
  python3 batch_chapter_forge.py
  python3 batch_chapter_forge.py --root /audiobooks --concurrency 1 --limit 20
  python3 batch_chapter_forge.py --dry-run

Environment:
  LIBRAFORGE_URL          default http://127.0.0.1:5056
  AUDIOBOOKS_ROOT         default /audiobooks
  BATCH_CHAPTER_CONCURRENCY  default 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav", ".wma", ".aac", ".mp4"}
SKIP_DIR_NAMES = {
    ".unorganized",
    "_unorganized",
    "unorganized",
    ".git",
    "-tmpfiles",
    "lost+found",
}
_ASIN_RE = re.compile(r"^(?:B[\dA-Z]{9}|\d{9}[\dX])$", re.IGNORECASE)
_ASIN_SENTINELS = frozenset({"", "HAS_ASIN", "NOREALASIN", "NONE", "NULL", "N/A"})
# LibraForge ``_FILENAME_ASIN_RE`` / ``_ASIN_TAG_KEYS`` — same ownership sources.
_FILENAME_ASIN_RE = re.compile(r"\[(?:ASIN\.)?([Bb]0[A-Z0-9]{8})\]", re.IGNORECASE)
_ASIN_TAG_KEYS = (
    "----:com.apple.iTunes:asin",
    "----:com.pilabor.tone:AUDIBLE_ASIN",
    "----:com.apple.iTunes:ASIN",
)

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def normalize_asin(value: Any) -> str:
    asin = str(value or "").strip().upper()
    if not asin or asin in _ASIN_SENTINELS:
        return ""
    if not _ASIN_RE.match(asin):
        return ""
    return asin


def _asin_from_sidecar(data: dict[str, Any]) -> str:
    if "sidecar" in data and isinstance(data["sidecar"], dict):
        data = data["sidecar"]
    book = data.get("book") if isinstance(data.get("book"), dict) else {}
    marker = data.get("marker") if isinstance(data.get("marker"), dict) else {}
    audible = data.get("audible") if isinstance(data.get("audible"), dict) else {}
    if not audible:
        audible = marker.get("audible") if isinstance(marker.get("audible"), dict) else {}
    backup = data.get("backup") if isinstance(data.get("backup"), dict) else {}
    applied = backup.get("applied_tags") if isinstance(backup.get("applied_tags"), dict) else {}
    scan_cache = data.get("scan_cache") if isinstance(data.get("scan_cache"), dict) else {}
    for candidate in (
        book.get("asin"),
        scan_cache.get("asin"),  # LF complete-metadata source (embedded-tag cache)
        audible.get("asin"),
        applied.get("asin"),
        marker.get("asin"),
        data.get("asin"),
    ):
        asin = normalize_asin(candidate)
        if asin:
            return asin
    return ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _asin_from_filename(path: Path) -> str:
    match = _FILENAME_ASIN_RE.search(path.name)
    return normalize_asin(match.group(1)) if match else ""


def _asin_from_embedded_audio(audio_file: Path) -> str:
    """Embedded ASIN via mutagen when available (same keys as LibraForge)."""
    try:
        from mutagen.id3 import ID3  # type: ignore
        from mutagen.mp4 import MP4, MP4FreeForm  # type: ignore
    except ImportError:
        return ""
    try:
        suffix = audio_file.suffix.lower()
        if suffix in {".m4b", ".m4a", ".mp4"}:
            tags = MP4(str(audio_file)).tags or {}
            for key in _ASIN_TAG_KEYS:
                raw = tags.get(key, [])
                if not raw:
                    continue
                val = raw[0]
                text = (
                    bytes(val).decode("utf-8", errors="ignore")
                    if isinstance(val, (bytes, bytearray, MP4FreeForm))
                    else str(val)
                ).strip()
                asin = normalize_asin(text)
                if asin:
                    return asin
        elif suffix == ".mp3":
            tags = ID3(str(audio_file))
            for key in tags.keys():
                if "asin" not in key.lower():
                    continue
                frame = tags.get(key)
                text = "".join(getattr(frame, "text", []) or []) if frame else ""
                asin = normalize_asin(text)
                if asin:
                    return asin
    except Exception:
        return ""
    return ""


def extract_asin(folder: Path) -> str:
    """Resolve ASIN the way LibraForge inventory does.

    filename ``[B0…]`` → libraforge.json (book / **scan_cache** / audible) →
    metadata.json → embedded mutagen tags.
    """
    audio_files = collect_audio(folder)
    for audio in audio_files:
        asin = _asin_from_filename(audio)
        if asin:
            return asin
    for name in ("libraforge.json",):
        path = folder / name
        if path.is_file():
            asin = _asin_from_sidecar(read_json(path))
            if asin:
                return asin
    for path in sorted(folder.glob("*.libraforge.json")):
        asin = _asin_from_sidecar(read_json(path))
        if asin:
            return asin
    meta = folder / "metadata.json"
    if meta.is_file():
        asin = normalize_asin(read_json(meta).get("asin"))
        if asin:
            return asin
    for path in sorted(folder.glob("*.metadata.json")):
        asin = normalize_asin(read_json(path).get("asin"))
        if asin:
            return asin
    for audio in audio_files:
        asin = _asin_from_embedded_audio(audio)
        if asin:
            return asin
    return ""


def collect_audio(folder: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                if "-tmpfiles" in path.parts:
                    continue
                files.append(path)
    except OSError:
        return []
    return files


def primary_audio(folder: Path) -> Path | None:
    audio = collect_audio(folder)
    if not audio:
        return None
    m4bs = [f for f in audio if f.suffix.lower() == ".m4b"]
    pool = m4bs or audio
    return max(pool, key=lambda p: p.stat().st_size if p.is_file() else 0)


def looks_like_book_folder(folder: Path) -> bool:
    """True when this folder directly contains audiobook audio (not just authors)."""
    return bool(collect_audio(folder))


def inventory_books(root: Path) -> list[Path]:
    books: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        # Prune staging / junk while walking
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIR_NAMES
            and not d.startswith(".")
            and "-tmpfiles" not in d
        ]
        folder = Path(dirpath)
        if folder == root:
            continue
        if looks_like_book_folder(folder):
            books.append(folder)
            # Don't descend into chapter-part trees once we have audio here
            dirnames[:] = []
    books.sort(key=lambda p: str(p).lower())
    return books


def http_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Unreachable {url}: {e}") from e
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def wait_for_run(
    base_url: str,
    run_id: str,
    *,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    terminal = {"completed", "failed", "cancelled", "canceled", "success", "error", "done"}
    while True:
        state = http_json("GET", f"{base_url}/api/runs/{run_id}", timeout=60.0)
        status = str(state.get("status") or "").lower()
        if state.get("done") is True or status in terminal:
            return state
        if time.time() >= deadline:
            raise RuntimeError(f"run {run_id} timed out after {timeout_seconds}s")
        time.sleep(poll_seconds)



def _ffprobe_cmd() -> list[str]:
    """Prefer host ffprobe; fall back to LibraForge container."""
    import shutil
    if shutil.which("ffprobe"):
        return ["ffprobe"]
    # Common on Pi: ffprobe only inside libraforge
    return ["docker", "exec", "libraforge", "ffprobe"]


def probe_embedded_chapters(audio: Path | None, *, root: Path, docker_root: str) -> dict[str, Any]:
    """Return embedded chapter count + title samples via ffprobe (stream metadata only)."""
    empty: dict[str, Any] = {"count": 0, "titles_head": [], "numeric_like": 0, "error": ""}
    if audio is None or not audio.is_file():
        empty["error"] = "no audio"
        return empty
    probe_path = str(audio)
    cmd_prefix = _ffprobe_cmd()
    if cmd_prefix[0] == "docker":
        probe_path = docker_path(audio, root, docker_root)
    try:
        out = subprocess.check_output(
            cmd_prefix
            + ["-v", "error", "-show_chapters", "-print_format", "json", probe_path],
            text=True,
            timeout=180,
            stderr=subprocess.STDOUT,
        )
        data = json.loads(out)
    except Exception as e:
        empty["error"] = str(e)[:300]
        return empty
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(chapters, list):
        return empty
    titles: list[str] = []
    for ch in chapters:
        tags = ch.get("tags") if isinstance(ch, dict) else None
        title = ""
        if isinstance(tags, dict):
            title = str(tags.get("title") or "")
        titles.append(title)
    numeric = 0
    for i, t in enumerate(titles, 1):
        ts = t.strip()
        if re.fullmatch(r"\d+", ts) or ts == str(i) or re.fullmatch(rf"Chapter\s*{i}", ts, re.I):
            numeric += 1
    return {
        "count": len(titles),
        "titles_head": titles[:8],
        "numeric_like": numeric,
        "error": "",
    }


def is_no_audible_chapters_error(msg: str) -> bool:
    lower = (msg or "").lower()
    return (
        "audible has no verified chapter" in lower
        or "no verified chapter data" in lower
        or "no chapter data" in lower
        or "chapters not found" in lower
    )


def run_chapter_forge(
    base_url: str,
    source_path: str,
    asin: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = http_json(
        "POST",
        f"{base_url}/api/chaptering/runs",
        {
            "source_path": source_path,
            "backend": "audible-chapters",
            "asin": asin,
            "no_save": False,
        },
        timeout=60.0,
    )
    run_id = str(started.get("id") or "").strip()
    if not run_id:
        raise RuntimeError(f"no run id: {started}")
    return wait_for_run(base_url, run_id, timeout_seconds=timeout_seconds)


def docker_path(host_path: Path, root: Path, docker_root: str) -> str:
    """Map host library path into LibraForge container path (/audiobooks/...)."""
    try:
        rel = host_path.resolve().relative_to(root.resolve())
    except ValueError:
        return host_path.as_posix()
    return f"{docker_root.rstrip('/')}/{rel.as_posix()}"


def _norm_lib_path(path: str | Path) -> str:
    text = str(path).replace("\\", "/")
    for prefix in ("/mnt/Audiobooks", "/audiobooks"):
        if text.startswith(prefix):
            text = "/audiobooks" + text[len(prefix) :]
            break
    return text.rstrip("/").lower()


def load_prior_success_paths(report_path: Path) -> set[str]:
    """Paths already chapterized successfully in a prior batch report."""
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return set()
    out: set[str] = set()
    for row in results:
        if not isinstance(row, dict) or row.get("status") != "success":
            continue
        for key in ("path", "audio", "source_path", "embedded_into"):
            val = row.get(key)
            if val:
                out.add(_norm_lib_path(val))
    return out


def process_book(
    folder: Path,
    *,
    root: Path,
    base_url: str,
    docker_root: str,
    dry_run: bool,
    timeout_seconds: float,
    skip_paths: set[str] | None = None,
) -> dict[str, Any]:
    asin = extract_asin(folder)
    audio = primary_audio(folder)
    row: dict[str, Any] = {
        "path": str(folder),
        "audio": str(audio) if audio else "",
        "asin": asin,
        "status": "pending",
        "chapters": 0,
        "chapters_before": 0,
        "chapters_after": 0,
        "titles_before_head": [],
        "titles_after_head": [],
        "numeric_before": 0,
        "numeric_after": 0,
        "error": "",
    }
    skip_paths = skip_paths or set()
    if (
        _norm_lib_path(folder) in skip_paths
        or (audio and _norm_lib_path(audio) in skip_paths)
    ):
        row["status"] = "skipped_already_done"
        return row
    if not asin:
        row["status"] = "skipped_no_asin"
        return row
    if audio is None:
        row["status"] = "skipped_no_audio"
        row["error"] = "no audio files in folder"
        return row

    source = docker_path(audio, root, docker_root)
    row["source_path"] = source
    before = probe_embedded_chapters(audio, root=root, docker_root=docker_root)
    row["chapters_before"] = int(before.get("count") or 0)
    row["titles_before_head"] = before.get("titles_head") or []
    row["numeric_before"] = int(before.get("numeric_like") or 0)
    if dry_run:
        row["status"] = "dry_run"
        return row

    try:
        report = run_chapter_forge(
            base_url, source, asin, timeout_seconds=timeout_seconds
        )
        status = str(report.get("status") or "").lower()
        err = str(report.get("error") or report.get("phase_detail") or status)
        if status in {"failed", "error", "cancelled", "canceled"} or report.get("error"):
            if is_no_audible_chapters_error(err):
                row["status"] = "skipped_no_audible_chapters"
            else:
                row["status"] = "failed"
            row["error"] = err[:500]
            after = probe_embedded_chapters(audio, root=root, docker_root=docker_root)
            row["chapters_after"] = int(after.get("count") or 0)
            row["titles_after_head"] = after.get("titles_head") or []
            row["numeric_after"] = int(after.get("numeric_like") or 0)
            return row
        stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
        try:
            row["chapters"] = int(stats.get("chapters") or 0)
        except (TypeError, ValueError):
            row["chapters"] = 0
        row["embedded_into"] = stats.get("embedded_into") or ""
        after = probe_embedded_chapters(audio, root=root, docker_root=docker_root)
        row["chapters_after"] = int(after.get("count") or 0)
        row["titles_after_head"] = after.get("titles_head") or []
        row["numeric_after"] = int(after.get("numeric_like") or 0)
        if not row["chapters"] and row["chapters_after"]:
            row["chapters"] = row["chapters_after"]
        row["status"] = "success"
        return row
    except Exception as e:
        err = str(e)
        if is_no_audible_chapters_error(err):
            row["status"] = "skipped_no_audible_chapters"
        else:
            row["status"] = "failed"
        row["error"] = err[:500]
        after = probe_embedded_chapters(audio, root=root, docker_root=docker_root)
        row["chapters_after"] = int(after.get("count") or 0)
        row["titles_after_head"] = after.get("titles_head") or []
        row["numeric_after"] = int(after.get("numeric_like") or 0)
        return row


def trigger_abs_scan(base_url: str) -> str:
    """Best-effort ABS library scan (direct ABS API, then LibraForge/Library Site)."""
    abs_url = (os.environ.get("ABS_URL") or "").rstrip("/")
    abs_key = (os.environ.get("ABS_API_KEY") or "").strip()
    abs_lib = (os.environ.get("ABS_LIBRARY_ID") or "").strip()
    if abs_url and abs_key and abs_lib:
        try:
            req = urllib.request.Request(
                f"{abs_url}/api/libraries/{abs_lib}/scan",
                data=b"",
                headers={
                    "Authorization": f"Bearer {abs_key}",
                    "Accept": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                resp.read()
            return f"ABS {abs_url}/api/libraries/{abs_lib}/scan"
        except Exception as e:
            log(f"Direct ABS scan failed: {e}")
    for path in ("/api/abs/scan", "/api/abs/scan-library"):
        try:
            http_json("POST", f"{base_url}{path}", {}, timeout=60.0)
            return f"libraforge:{path}"
        except Exception:
            continue
    site = (os.environ.get("LIBRARY_SITE_URL") or "http://127.0.0.1:8000").rstrip("/")
    try:
        http_json("POST", f"{site}/api/library/abs/scan?wait=false", {}, timeout=60.0)
        return f"library-site:{site}/api/library/abs/scan"
    except Exception as e:
        log(f"Library Site ABS scan failed: {e}")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch Chapter Forge (Audible chapters)")
    parser.add_argument(
        "--root",
        default=os.environ.get("AUDIOBOOKS_ROOT", "/audiobooks"),
        help="Library root (host path visible to this script)",
    )
    parser.add_argument(
        "--docker-root",
        default="/audiobooks",
        help="Same library as seen by LibraForge container",
    )
    parser.add_argument(
        "--libraforge-url",
        default=os.environ.get("LIBRAFORGE_URL", "http://127.0.0.1:5056").rstrip("/"),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("BATCH_CHAPTER_CONCURRENCY", "1")),
    )
    parser.add_argument("--limit", type=int, default=0, help="Max books to process (0=all)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Per-book Chapter Forge timeout seconds",
    )
    parser.add_argument(
        "--reports-dir",
        default=os.environ.get("REPORTS_DIR", "/opt/stacks/libraforge/reports"),
    )
    parser.add_argument("--skip-abs-scan", action="store_true")
    parser.add_argument(
        "--skip-success-from",
        default="",
        help="Prior batch report JSON; skip paths that already succeeded",
    )
    parser.add_argument(
        "--only-with-asin",
        action="store_true",
        help="Inventory all folders but only process those with a resolvable ASIN",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        log(f"ERROR: root not found: {root}")
        return 2

    skip_paths: set[str] = set()
    if args.skip_success_from:
        prior = Path(args.skip_success_from)
        skip_paths = load_prior_success_paths(prior)
        log(f"Skipping {len(skip_paths)} path key(s) from prior success report {prior}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    reports = Path(args.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"chapter-forge-batch-{stamp}.json"
    log_path = reports / f"chapter-forge-batch-{stamp}.log"

    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, data):
            for s in self.streams:
                s.write(data)
                s.flush()

        def flush(self):
            for s in self.streams:
                s.flush()

    log_fh = log_path.open("w", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_fh)  # type: ignore[assignment]
    sys.stderr = Tee(sys.__stderr__, log_fh)  # type: ignore[assignment]

    log(f"Inventory under {root} …")
    books = inventory_books(root)
    log(f"Found {len(books)} book folder(s) with audio")
    if args.only_with_asin:
        with_asin = [b for b in books if extract_asin(b)]
        log(f"With resolvable ASIN: {len(with_asin)} (of {len(books)})")
        books = with_asin
    if skip_paths:
        before = len(books)
        books = [
            b
            for b in books
            if _norm_lib_path(b) not in skip_paths
            and not (
                (audio := primary_audio(b)) and _norm_lib_path(audio) in skip_paths
            )
        ]
        log(f"After skipping prior successes: {len(books)} (removed {before - len(books)})")
    if args.limit and args.limit > 0:
        books = books[: args.limit]
        log(f"Limited to first {len(books)}")

    concurrency = max(1, min(int(args.concurrency or 1), 2))
    results: list[dict[str, Any]] = []
    counts = {
        "success": 0,
        "failed": 0,
        "skipped_no_asin": 0,
        "skipped_no_audio": 0,
        "skipped_already_done": 0,
        "skipped_no_audible_chapters": 0,
        "dry_run": 0,
        "other": 0,
    }

    def _work(folder: Path) -> dict[str, Any]:
        row = process_book(
            folder,
            root=root,
            base_url=args.libraforge_url,
            docker_root=args.docker_root,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout,
            skip_paths=skip_paths,
        )
        log(
            f"[{row['status']}] asin={row.get('asin') or '-'} "
            f"chapters={row.get('chapters_before', 0)}->{row.get('chapters_after') or row.get('chapters', 0)} "
            f"{row['path']}"
            + (f" err={row['error']}" if row.get("error") else "")
        )
        return row

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_work, book): book for book in books}
        for fut in as_completed(futures):
            row = fut.result()
            results.append(row)
            key = row["status"] if row["status"] in counts else "other"
            counts[key] = counts.get(key, 0) + 1

    results.sort(key=lambda r: r.get("path") or "")
    abs_scan = ""
    if not args.dry_run and not args.skip_abs_scan:
        abs_scan = trigger_abs_scan(args.libraforge_url)
        if abs_scan:
            log(f"Triggered ABS scan via LibraForge {abs_scan}")
        else:
            log("ABS scan endpoint not available on LibraForge — trigger from Library Admin if needed")

    summary = {
        "stamp": stamp,
        "root": str(root),
        "libraforge_url": args.libraforge_url,
        "dry_run": args.dry_run,
        "concurrency": concurrency,
        "force_reembed": not bool(args.skip_success_from),
        "backend": "audible-chapters",
        "skip_success_from": args.skip_success_from or "",
        "counts": counts,
        "total": len(results),
        "abs_scan": abs_scan,
        "results": results,
    }
    report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    # Mirror into Library Site reports/ when running on Pi under libraforge reports
    mirror = Path("/opt/stacks/Library Site/reports") / report_path.name
    try:
        if mirror.parent.is_dir() and mirror.resolve() != report_path.resolve():
            mirror.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            log(f"Mirrored report to {mirror}")
    except OSError:
        pass
    log(
        f"Done. success={counts['success']} failed={counts['failed']} "
        f"skipped_no_asin={counts['skipped_no_asin']} "
        f"skipped_no_audible_chapters={counts.get('skipped_no_audible_chapters', 0)} "
        f"skipped_already_done={counts['skipped_already_done']} "
        f"skipped_no_audio={counts['skipped_no_audio']} "
        f"report={report_path}"
    )
    # Soft-continue: non-zero only on total catastrophe (no results). Failures are OK.
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
