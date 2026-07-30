#!/usr/bin/env python3
"""Remove duplicate audiobook leftovers left by Library Sweep (dry-run by default).

Keeps one .m4b per book folder (newest/largest preferred).
Removes sibling .m4b files and leftover multiparts when a .m4b exists.
Dedupes hardlinked .m4b copies across folders (same inode).
Prunes empty parent dirs under the library root.

Usage:
  python3 scripts/cleanup_sweep_duplicates.py /mnt/Audiobooks
  python3 scripts/cleanup_sweep_duplicates.py /mnt/Audiobooks --apply
"""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from pathlib import Path

AUDIO_PARTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".wav"}
SKIP_DIRNAMES = {".unorganized", "_unorganized", ".git", "@eaDir"}
ASIN_IN_NAME = re.compile(r"\[(?:ASIN\.)?[Bb]0[A-Z0-9]{8}\]", re.IGNORECASE)


def iter_book_dirs(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRNAMES and not d.startswith(".")]
        p = Path(dirpath)
        if p == root:
            continue
        # book-ish: contains audio
        names = [f for f in filenames if not f.startswith(".")]
        exts = {Path(f).suffix.lower() for f in names}
        if ".m4b" in exts or (exts & AUDIO_PARTS):
            yield p, names


def pick_keeper(m4bs: list[Path]) -> Path:
    def score(p: Path):
        try:
            st = p.stat()
            size = st.st_size
            mtime = st.st_mtime
        except OSError:
            size, mtime = 0, 0
        # Prefer forged / ASIN-named paths, then larger, then newer.
        asin = 1 if ASIN_IN_NAME.search(p.name) or ASIN_IN_NAME.search(str(p.parent)) else 0
        return (asin, size, mtime, len(p.name))

    return max(m4bs, key=score)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("library_root", type=Path)
    ap.add_argument("--apply", action="store_true", help="Actually delete (default dry-run)")
    args = ap.parse_args()
    root = args.library_root.resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}")
        return 1

    deleted = 0
    kept = 0
    inode_groups: dict[tuple[int, int], list[Path]] = defaultdict(list)

    for book_dir, names in iter_book_dirs(root):
        files = [book_dir / n for n in names]
        m4bs = [f for f in files if f.suffix.lower() == ".m4b"]
        parts = [f for f in files if f.suffix.lower() in AUDIO_PARTS]
        to_delete: list[Path] = []
        if len(m4bs) >= 2:
            keeper = pick_keeper(m4bs)
            kept += 1
            for f in m4bs:
                if f != keeper:
                    to_delete.append(f)
            # If we have a keeper m4b, multiparts are leftovers.
            to_delete.extend(parts)
        elif len(m4bs) == 1 and parts:
            kept += 1
            to_delete.extend(parts)

        for f in m4bs:
            try:
                st = f.stat()
                inode_groups[(st.st_dev, st.st_ino)].append(f)
            except OSError:
                pass

        for f in to_delete:
            rel = f.relative_to(root)
            if args.apply:
                try:
                    f.unlink()
                    print(f"DELETED {rel}")
                    deleted += 1
                except OSError as e:
                    print(f"FAIL {rel}: {e}")
            else:
                print(f"WOULD DELETE {rel}")
                deleted += 1

    # Cross-folder hardlink duplicates: keep one path, unlink the rest.
    for key, paths in inode_groups.items():
        uniq = []
        seen = set()
        for p in paths:
            rp = str(p.resolve())
            if rp in seen:
                continue
            seen.add(rp)
            if p.exists():
                uniq.append(p)
        if len(uniq) < 2:
            continue
        keeper = pick_keeper(uniq)
        for f in uniq:
            if f == keeper:
                continue
            rel = f.relative_to(root)
            if args.apply:
                try:
                    f.unlink()
                    print(f"DELETED HARDLINK DUP {rel} (kept {keeper.relative_to(root)})")
                    deleted += 1
                except OSError as e:
                    print(f"FAIL {rel}: {e}")
            else:
                print(f"WOULD DELETE HARDLINK DUP {rel} (keep {keeper.relative_to(root)})")
                deleted += 1

    # Prune empty dirs (deepest first)
    empty = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        if any(part in SKIP_DIRNAMES for part in p.relative_to(root).parts):
            continue
        try:
            if not any(p.iterdir()):
                empty.append(p)
        except OSError:
            pass
    for p in empty:
        rel = p.relative_to(root)
        if args.apply:
            try:
                p.rmdir()
                print(f"REMOVED EMPTY {rel}")
            except OSError as e:
                print(f"FAIL EMPTY {rel}: {e}")
        else:
            print(f"WOULD REMOVE EMPTY {rel}")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] duplicate clean candidates={deleted} folders_with_keeper={kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
