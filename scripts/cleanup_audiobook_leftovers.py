#!/usr/bin/env python3
"""One-time / ops cleanup of leftover multipart sources after M4B conversion.

Scans an audiobook library for:
  - Tape*/Part*/CD* (and format-variant) multiparts when a good .m4b exists
  - Empty directories
  - Stale empty req_* under .unorganized
  - Obvious temps (*.tmp, *.m4b.part, *-tmpfiles)

By default multiparts are moved to a quarantine folder. Pass --hard-delete to
unlink instead when an m4b clearly exists and passes the size sanity check.

Never deletes the sole audio in a folder with no .m4b. Never touches eBooks
except optional staging junk under unorganized when --include-ebook-staging.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav", ".wma", ".aac", ".mp4"}
FORMAT_DIR_NAMES = frozenset({
    "mp3", "m4a", "m4b", "aac", "flac", "ogg", "opus", "wma", "wav", "audio", "audiobook",
})
MULTIPART_DIR_RE = re.compile(
    r"^(?:tape|part|cd|disc|disk|chapter|ch|track)\s*\d+$",
    re.IGNORECASE,
)
REQ_DIR_RE = re.compile(r"^req_\d+", re.IGNORECASE)
SKIP_DIR_NAMES = frozenset({
    ".git", ".recycle", "#recycle", "@eaDir", ".Trash", ".trash",
    "lost+found", "System Volume Information",
})
# Minimum size for a "real" m4b (stubs / failed merges are often tiny).
DEFAULT_MIN_M4B_BYTES = 5 * 1024 * 1024  # 5 MiB

logger = logging.getLogger("cleanup_audiobook_leftovers")


@dataclass
class CleanupReport:
    started_at: str
    library_root: str
    quarantine_root: str | None
    hard_delete: bool
    dry_run: bool
    min_m4b_bytes: int
    files_quarantined: int = 0
    files_deleted: int = 0
    dirs_removed: int = 0
    bytes_freed: int = 0
    skipped_review: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_action(self, action: str, path: Path, *, bytes_: int = 0, note: str = "") -> None:
        entry = {"action": action, "path": str(path), "bytes": bytes_, "note": note}
        self.actions.append(entry)
        logger.info("%s %s (%s bytes) %s", action, path, bytes_, note)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _collect_audio(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    out: list[Path] = []
    try:
        for f in folder.rglob("*"):
            if not f.is_file():
                continue
            if "-tmpfiles" in f.parts:
                continue
            if f.suffix.lower() in AUDIO_EXTS:
                out.append(f)
    except OSError:
        return []
    return out


def _good_m4bs(folder: Path, min_bytes: int) -> list[Path]:
    good: list[Path] = []
    for f in _collect_audio(folder):
        if f.suffix.lower() != ".m4b":
            continue
        try:
            if f.stat().st_size >= min_bytes:
                good.append(f)
        except OSError:
            continue
    return good


def _quarantine_tree(
    path: Path,
    *,
    library_root: Path,
    report: CleanupReport,
    quarantine_root: Path | None,
    hard_delete: bool,
    dry_run: bool,
    reason: str,
) -> None:
    """Move/delete a file or directory, preserving relative path under quarantine."""
    try:
        size = path.stat().st_size if path.is_file() else _dir_size(path)
    except OSError as e:
        report.errors.append(f"stat failed {path}: {e}")
        return

    if dry_run:
        report.add_action("would_remove", path, bytes_=size, note=reason)
        report.bytes_freed += size
        if path.is_dir():
            report.dirs_removed += 1
        else:
            if hard_delete or quarantine_root is None:
                report.files_deleted += 1
            else:
                report.files_quarantined += 1
        return

    try:
        if hard_delete or quarantine_root is None:
            if path.is_dir():
                shutil.rmtree(path)
                report.dirs_removed += 1
                report.add_action("deleted", path, bytes_=size, note=reason)
            else:
                path.unlink()
                report.files_deleted += 1
                report.add_action("deleted", path, bytes_=size, note=reason)
            report.bytes_freed += size
            return

        try:
            rel = path.resolve().relative_to(library_root.resolve())
        except ValueError:
            rel = Path(path.name)
        dest = quarantine_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.parent / f"{dest.name}__{datetime.now(timezone.utc).strftime('%H%M%S')}"
        shutil.move(str(path), str(dest))
        report.bytes_freed += size
        if dest.is_dir():
            report.dirs_removed += 1
        else:
            report.files_quarantined += 1
        report.add_action("quarantined", path, bytes_=size, note=f"{reason} -> {dest}")
    except OSError as e:
        report.errors.append(f"remove failed {path}: {e}")


def cleanup_temps(library_root: Path, report: CleanupReport, *, dry_run: bool, hard_delete: bool, quarantine_root: Path | None) -> None:
    patterns = ("*.tmp", "*.m4b.part", "*.m4b.tmp", "*.mp3.part", "*.m4a.part")
    for pattern in patterns:
        for path in library_root.rglob(pattern):
            if not path.is_file():
                continue
            if any(p in SKIP_DIR_NAMES for p in path.parts):
                continue
            _quarantine_tree(
                path,
                library_root=library_root,
                report=report,
                quarantine_root=quarantine_root,
                hard_delete=hard_delete,
                dry_run=dry_run,
                reason=f"temp file ({pattern})",
            )
    for d in list(library_root.rglob("*-tmpfiles")):
        if d.is_dir():
            _quarantine_tree(
                d,
                library_root=library_root,
                report=report,
                quarantine_root=quarantine_root,
                hard_delete=True,  # temps: hard delete even in quarantine mode
                dry_run=dry_run,
                reason="m4b-tool tmpfiles dir",
            )


def cleanup_multipart_when_m4b_exists(
    book_dir: Path,
    *,
    library_root: Path,
    report: CleanupReport,
    min_m4b_bytes: int,
    dry_run: bool,
    hard_delete: bool,
    quarantine_root: Path | None,
) -> None:
    m4bs = _good_m4bs(book_dir, min_m4b_bytes)
    if not m4bs:
        # Tiny m4b + parts → human review
        tiny = [
            f for f in _collect_audio(book_dir)
            if f.suffix.lower() == ".m4b"
        ]
        parts = [
            f for f in _collect_audio(book_dir)
            if f.suffix.lower() != ".m4b"
        ]
        if tiny and parts:
            report.skipped_review.append({
                "path": str(book_dir),
                "reason": "m4b present but below min size with leftover parts",
                "m4b_sizes": [f.stat().st_size for f in tiny if f.exists()],
                "part_count": len(parts),
            })
        return

    largest_m4b = max(m4bs, key=lambda p: p.stat().st_size)
    m4b_size = largest_m4b.stat().st_size

    # 1) Multipart subdirs (Tape1, Part 2, CD1, …)
    try:
        children = list(book_dir.iterdir())
    except OSError:
        return

    for child in children:
        if not child.is_dir():
            continue
        name = child.name
        is_multipart = bool(MULTIPART_DIR_RE.match(name))
        is_format = name.lower() in FORMAT_DIR_NAMES
        if not (is_multipart or is_format):
            continue
        audio_in = _collect_audio(child)
        if not audio_in:
            # empty format/multipart dir — prune later
            continue
        if any(a.suffix.lower() == ".m4b" for a in audio_in):
            # format dir that holds the m4b itself — only strip non-m4b inside
            for a in audio_in:
                if a.suffix.lower() == ".m4b":
                    continue
                _quarantine_tree(
                    a,
                    library_root=library_root,
                    report=report,
                    quarantine_root=quarantine_root,
                    hard_delete=hard_delete,
                    dry_run=dry_run,
                    reason=f"source part beside good m4b in {child.name}",
                )
            continue

        parts_size = sum(a.stat().st_size for a in audio_in if a.is_file())
        # Sanity: don't remove a parts tree larger than 3x the m4b (odd dual editions)
        if parts_size > m4b_size * 3 and is_format:
            report.skipped_review.append({
                "path": str(child),
                "reason": "format/multipart tree much larger than m4b — possible unique edition",
                "parts_bytes": parts_size,
                "m4b_bytes": m4b_size,
            })
            continue

        _quarantine_tree(
            child,
            library_root=library_root,
            report=report,
            quarantine_root=quarantine_root,
            hard_delete=hard_delete,
            dry_run=dry_run,
            reason=f"multipart/format dir with good m4b ({largest_m4b.name})",
        )

    # 2) Loose source parts sitting next to the m4b in the book folder
    for a in _collect_audio(book_dir):
        if a.parent != book_dir:
            continue
        if a.suffix.lower() == ".m4b":
            continue
        _quarantine_tree(
            a,
            library_root=library_root,
            report=report,
            quarantine_root=quarantine_root,
            hard_delete=hard_delete,
            dry_run=dry_run,
            reason=f"loose source beside good m4b ({largest_m4b.name})",
        )


def cleanup_empty_req_dirs(unorganized: Path, report: CleanupReport, *, dry_run: bool) -> None:
    if not unorganized.is_dir():
        return
    try:
        children = list(unorganized.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or not REQ_DIR_RE.match(child.name):
            continue
        audio = _collect_audio(child)
        if audio:
            continue
        # empty or junk-only req_* → remove
        size = _dir_size(child)
        if dry_run:
            report.add_action("would_remove", child, bytes_=size, note="empty/junk req_* staging")
            report.dirs_removed += 1
            report.bytes_freed += size
            continue
        try:
            shutil.rmtree(child)
            report.dirs_removed += 1
            report.bytes_freed += size
            report.add_action("deleted", child, bytes_=size, note="empty/junk req_* staging")
        except OSError as e:
            report.errors.append(f"req wipe failed {child}: {e}")


def prune_empty_dirs(root: Path, report: CleanupReport, *, dry_run: bool, protect: set[Path]) -> None:
    try:
        dirs = sorted(
            (d for d in root.rglob("*") if d.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
    except OSError:
        return
    root_res = root.resolve()
    for d in dirs:
        try:
            d_res = d.resolve()
            if d_res == root_res or d_res in protect:
                continue
            if d.name in SKIP_DIR_NAMES:
                continue
            if not d.is_dir():
                continue
            try:
                next(d.iterdir())
                continue
            except StopIteration:
                pass
            if dry_run:
                report.add_action("would_rmdir", d, note="empty directory")
                report.dirs_removed += 1
                continue
            d.rmdir()
            report.dirs_removed += 1
            report.add_action("rmdir", d, note="empty directory")
        except OSError:
            continue


def find_book_dirs(library_root: Path) -> list[Path]:
    """Heuristic: dirs that directly contain audio, excluding staging roots' internals walk."""
    book_dirs: set[Path] = set()
    unorganized = (library_root / ".unorganized").resolve()
    for dirpath, dirnames, filenames in os.walk(library_root):
        p = Path(dirpath)
        # prune skip dirs in-place
        dirnames[:] = [
            n for n in dirnames
            if n not in SKIP_DIR_NAMES
            and not (n.startswith(".") and n != ".unorganized")
        ]
        # Don't treat quarantine-like names as books
        try:
            if p.resolve() == unorganized:
                continue
        except OSError:
            pass
        has_audio = any(Path(f).suffix.lower() in AUDIO_EXTS for f in filenames)
        if has_audio:
            book_dirs.add(p)
    return sorted(book_dirs)


def run_cleanup(
    library_root: Path,
    *,
    quarantine_base: Path | None,
    hard_delete: bool,
    dry_run: bool,
    min_m4b_bytes: int,
) -> CleanupReport:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    quarantine_root = None
    if quarantine_base and not hard_delete:
        quarantine_root = quarantine_base / f"cleanup-{stamp}"
        if not dry_run:
            quarantine_root.mkdir(parents=True, exist_ok=True)

    report = CleanupReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        library_root=str(library_root),
        quarantine_root=str(quarantine_root) if quarantine_root else None,
        hard_delete=hard_delete,
        dry_run=dry_run,
        min_m4b_bytes=min_m4b_bytes,
    )

    logger.info(
        "Scanning %s (dry_run=%s hard_delete=%s min_m4b=%s)",
        library_root,
        dry_run,
        hard_delete,
        min_m4b_bytes,
    )

    # Temps library-wide (including .unorganized)
    cleanup_temps(
        library_root,
        report,
        dry_run=dry_run,
        hard_delete=hard_delete,
        quarantine_root=quarantine_root,
    )

    # Empty/junk req_* under staging
    cleanup_empty_req_dirs(
        library_root / ".unorganized",
        report,
        dry_run=dry_run,
    )
    legacy = library_root / "_unorganized"
    if legacy.is_dir():
        cleanup_empty_req_dirs(legacy, report, dry_run=dry_run)

    # Book folders with good m4b + leftover parts
    for book_dir in find_book_dirs(library_root):
        try:
            if ".unorganized" in book_dir.parts or "_unorganized" in book_dir.parts:
                # Staging: only clean multiparts when good m4b already in same folder
                pass
            cleanup_multipart_when_m4b_exists(
                book_dir,
                library_root=library_root,
                report=report,
                min_m4b_bytes=min_m4b_bytes,
                dry_run=dry_run,
                hard_delete=hard_delete,
                quarantine_root=quarantine_root,
            )
        except Exception as e:
            report.errors.append(f"book cleanup {book_dir}: {e}")

    protect = {library_root.resolve()}
    u = library_root / ".unorganized"
    if u.is_dir():
        protect.add(u.resolve())
    prune_empty_dirs(library_root, report, dry_run=dry_run, protect=protect)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--library",
        type=Path,
        default=Path("/mnt/Audiobooks"),
        help="Audiobook library root",
    )
    parser.add_argument(
        "--quarantine",
        type=Path,
        default=Path("/mnt/m4b-source-quarantine"),
        help="Quarantine base (cleanup-YYYYMMDD created underneath)",
    )
    parser.add_argument(
        "--hard-delete",
        action="store_true",
        help="Hard-delete instead of quarantine (only when m4b clearly present)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions only")
    parser.add_argument(
        "--min-m4b-bytes",
        type=int,
        default=DEFAULT_MIN_M4B_BYTES,
        help="Minimum .m4b size to treat as a real convert (default 5 MiB)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        action="append",
        default=[],
        help="Write JSON report to this path (repeatable)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    library = args.library
    if not library.is_dir():
        logger.error("Library root does not exist: %s", library)
        return 2

    report = run_cleanup(
        library,
        quarantine_base=None if args.hard_delete else args.quarantine,
        hard_delete=args.hard_delete,
        dry_run=args.dry_run,
        min_m4b_bytes=args.min_m4b_bytes,
    )

    payload = asdict(report)
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    payload["bytes_freed_human"] = _human_bytes(report.bytes_freed)

    text = json.dumps(payload, indent=2)
    print(text)

    for dest in args.report:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            logger.info("Wrote report %s", dest)
        except OSError as e:
            logger.error("Could not write report %s: %s", dest, e)
            return 1
    return 0 if not report.errors else 1


def _human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(x) < 1024 or unit == "TiB":
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{n} B"


if __name__ == "__main__":
    sys.exit(main())
