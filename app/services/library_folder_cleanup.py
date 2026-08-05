"""Library Sweep folder cleanup — classify/delete non-canonical leftovers.

Canonical layout (pipeline output):
  Audiobooks (under audiobook_dir):
    - Staging: ``.unorganized/`` / ``_unorganized/`` (protected entirely)
    - Book folders containing audio
    - Sidecars: ``metadata.json``, ``libraforge.json``,
      ``.m4b-tool-metadata.json``, cover images, ``*.nfo``
    - Audio: ``.m4b`` preferred; multiparts only when no ``.m4b`` exists

  Ebooks (under ebook_dir):
    - Staging: ``unorganized/`` (protected entirely)
    - Book folders containing ``ebook_applied.json`` and/or ebook files
    - Sidecars: ``ebook_applied.json``, cover images
    - Primary ebook file(s); numbered ``Title (N).ext`` duplicates are orphans
      when a clean canonical sibling exists

Cleanup only proposes deletions under the two library roots. Dry-run preview
first; apply requires an explicit confirm token from the preview response.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from app.config import get_settings
from app.services.ebook_pipeline import EBOOK_EXTENSIONS, ebook_staging_dirname
from app.services.forge_pipeline import AUDIO_EXTENSIONS, unorganized_dirnames

logger = logging.getLogger(__name__)
settings = get_settings()

Scope = Literal["audiobook", "ebook"]

COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_SIDECARS = {
    "metadata.json",
    "libraforge.json",
    ".m4b-tool-metadata.json",
}
EBOOK_SIDECARS = {"ebook_applied.json"}
SKIP_DIRNAMES_COMMON = {".git", "@eaDir", ".DS_Store", "lost+found"}

_DUP_SUFFIX = re.compile(r"(?:[-_ ](?:copy|\d+)| \(\d+\))$", re.IGNORECASE)
_PREVIEW_TTL_SEC = 30 * 60
_preview_cache: dict[str, dict[str, Any]] = {}


@dataclass
class CleanupCandidate:
    path: str
    kind: str
    reason: str
    size_bytes: int = 0
    scope: Scope = "audiobook"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "reason": self.reason,
            "size_bytes": int(self.size_bytes or 0),
            "scope": self.scope,
        }


@dataclass
class CleanupPreview:
    token: str
    candidates: list[CleanupCandidate] = field(default_factory=list)
    protected_roots: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        total_bytes = sum(c.size_bytes for c in self.candidates)
        return {
            "token": self.token,
            "scopes": list(self.scopes),
            "protected_roots": list(self.protected_roots),
            "count": len(self.candidates),
            "total_bytes": total_bytes,
            "candidates": [c.to_dict() for c in self.candidates],
            "canonical": canonical_layout_docs(),
            "expires_in_seconds": _PREVIEW_TTL_SEC,
        }


def canonical_layout_docs() -> dict[str, Any]:
    return {
        "audiobook": {
            "root": str(Path(settings.audiobook_dir)),
            "staging": sorted(unorganized_dirnames()),
            "book_layout": "{author}/{series} [{edition}]/{title}/",
            "kept_files": sorted(
                list(AUDIO_SIDECARS)
                + ["*.m4b / audio parts (when no m4b)", "cover images", "*.nfo"]
            ),
            "orphans": [
                "extra .m4b when a keeper exists",
                "multipart audio when a .m4b exists",
                "hardlinked duplicate .m4b copies across folders",
                "empty directories",
                "loose files at library root (non-staging)",
            ],
        },
        "ebook": {
            "root": str(Path(settings.ebook_dir)),
            "staging": [ebook_staging_dirname()],
            "book_layout": "{author}/{series} [{edition}]/{title}/",
            "kept_files": sorted(
                list(EBOOK_SIDECARS) + ["primary ebook", "cover images"]
            ),
            "orphans": [
                "numbered Title (N).ext when Title.ext exists",
                "loose files at library root",
                "empty folders",
                "junk files in book folders",
            ],
        },
    }


def _safe_size(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if path.is_dir():
            total = 0
            for p in path.rglob("*"):
                if p.is_file():
                    try:
                        total += int(p.stat().st_size)
                    except OSError:
                        pass
            return total
    except OSError:
        pass
    return 0


def iter_audiobook_book_dirs(root: Path) -> Iterable[Path]:
    """Yield directories that look like audiobook leaf folders (contain audio)."""
    skip = set(unorganized_dirnames()) | SKIP_DIRNAMES_COMMON
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        p = Path(dirpath)
        if p == root:
            continue
        names = [f for f in filenames if not f.startswith(".")]
        exts = {Path(f).suffix.lower() for f in names}
        if ".m4b" in exts or (exts & set(AUDIO_EXTENSIONS)):
            yield p


def iter_ebook_book_dirs(root: Path) -> Iterable[Path]:
    """Yield directories that look like ebook leaf folders."""
    staging = ebook_staging_dirname()
    skip = {staging} | SKIP_DIRNAMES_COMMON
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        p = Path(dirpath)
        if p == root:
            continue
        lower_names = {f.lower() for f in filenames}
        if "ebook_applied.json" in lower_names:
            yield p
            continue
        if any(Path(f).suffix.lower() in EBOOK_EXTENSIONS for f in filenames):
            yield p


def pick_m4b_keeper(m4bs: list[Path]) -> Path:
    def score(p: Path) -> tuple:
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        clean = 0 if _DUP_SUFFIX.search(p.stem) else 1
        depth = len(p.parts)
        return (clean, size, mtime, depth, -len(p.name))

    return max(m4bs, key=score)


def classify_audiobook_dir(book_dir: Path) -> list[CleanupCandidate]:
    """Classify orphan files inside one audiobook folder."""
    out: list[CleanupCandidate] = []
    try:
        files = [p for p in book_dir.iterdir() if p.is_file()]
    except OSError:
        return out

    m4bs = [p for p in files if p.suffix.lower() == ".m4b"]
    parts = [
        p
        for p in files
        if p.suffix.lower() in AUDIO_EXTENSIONS and p.suffix.lower() != ".m4b"
    ]

    if len(m4bs) >= 2:
        keeper = pick_m4b_keeper(m4bs)
        for f in m4bs:
            if f != keeper:
                out.append(
                    CleanupCandidate(
                        path=str(f),
                        kind="duplicate_m4b",
                        reason=f"Extra .m4b; keeping {keeper.name}",
                        size_bytes=_safe_size(f),
                        scope="audiobook",
                    )
                )
        for f in parts:
            out.append(
                CleanupCandidate(
                    path=str(f),
                    kind="multipart_leftover",
                    reason="Multipart audio leftover after .m4b exists",
                    size_bytes=_safe_size(f),
                    scope="audiobook",
                )
            )
    elif len(m4bs) == 1 and parts:
        for f in parts:
            out.append(
                CleanupCandidate(
                    path=str(f),
                    kind="multipart_leftover",
                    reason="Multipart audio leftover after .m4b exists",
                    size_bytes=_safe_size(f),
                    scope="audiobook",
                )
            )

    keep_names = {n.lower() for n in AUDIO_SIDECARS}
    marked = {c.path for c in out}
    for f in files:
        name_l = f.name.lower()
        suf = f.suffix.lower()
        if name_l in keep_names:
            continue
        if suf in AUDIO_EXTENSIONS or suf == ".m4b":
            continue
        if suf in COVER_EXTENSIONS or suf == ".nfo":
            continue
        if str(f) in marked:
            continue
        if name_l in {".ds_store", "thumbs.db", "desktop.ini"} or suf in {
            ".tmp",
            ".temp",
            ".part",
            ".download",
        }:
            out.append(
                CleanupCandidate(
                    path=str(f),
                    kind="junk_file",
                    reason="Non-canonical junk file in book folder",
                    size_bytes=_safe_size(f),
                    scope="audiobook",
                )
            )
    return out


def classify_ebook_dir(book_dir: Path) -> list[CleanupCandidate]:
    """Classify orphan files inside one ebook folder."""
    out: list[CleanupCandidate] = []
    try:
        files = [p for p in book_dir.iterdir() if p.is_file()]
    except OSError:
        return out

    ebooks = [p for p in files if p.suffix.lower() in EBOOK_EXTENSIONS]
    by_base: dict[str, list[Path]] = {}
    for f in ebooks:
        stem = f.stem
        base = _DUP_SUFFIX.sub("", stem).strip() or stem
        key = f"{base.lower()}::{f.suffix.lower()}"
        by_base.setdefault(key, []).append(f)

    for group in by_base.values():
        if len(group) < 2:
            continue

        def score(p: Path) -> tuple:
            clean = 0 if _DUP_SUFFIX.search(p.stem) else 1
            try:
                st = p.stat()
                return (clean, st.st_size, st.st_mtime)
            except OSError:
                return (clean, 0, 0.0)

        keeper = max(group, key=score)
        for f in group:
            if f == keeper:
                continue
            out.append(
                CleanupCandidate(
                    path=str(f),
                    kind="numbered_duplicate",
                    reason=f"Duplicate ebook; keeping {keeper.name}",
                    size_bytes=_safe_size(f),
                    scope="ebook",
                )
            )

    keep_names = {n.lower() for n in EBOOK_SIDECARS}
    marked = {c.path for c in out}
    for f in files:
        name_l = f.name.lower()
        suf = f.suffix.lower()
        if name_l in keep_names:
            continue
        if suf in EBOOK_EXTENSIONS:
            continue
        if suf in COVER_EXTENSIONS:
            continue
        if str(f) in marked:
            continue
        if name_l in {".ds_store", "thumbs.db", "desktop.ini"} or suf in {
            ".tmp",
            ".temp",
            ".part",
            ".download",
        }:
            out.append(
                CleanupCandidate(
                    path=str(f),
                    kind="junk_file",
                    reason="Non-canonical junk file in book folder",
                    size_bytes=_safe_size(f),
                    scope="ebook",
                )
            )
    return out


def _classify_hardlinked_m4b_dupes(book_dirs: Iterable[Path]) -> list[CleanupCandidate]:
    inode_groups: dict[tuple[int, int], list[Path]] = {}
    for book_dir in book_dirs:
        try:
            for p in book_dir.iterdir():
                if not p.is_file() or p.suffix.lower() != ".m4b":
                    continue
                st = p.stat()
                if st.st_nlink < 2:
                    continue
                inode_groups.setdefault((st.st_dev, st.st_ino), []).append(p)
        except OSError:
            continue

    out: list[CleanupCandidate] = []
    for paths in inode_groups.values():
        if len(paths) < 2:
            continue
        keeper = pick_m4b_keeper(paths)
        for p in paths:
            if p == keeper:
                continue
            out.append(
                CleanupCandidate(
                    path=str(p),
                    kind="hardlink_duplicate",
                    reason=f"Hardlinked duplicate of {keeper}",
                    size_bytes=0,
                    scope="audiobook",
                )
            )
    return out


def _root_level_orphans(
    root: Path, *, scope: Scope, staging_names: set[str]
) -> list[CleanupCandidate]:
    out: list[CleanupCandidate] = []
    if not root.is_dir():
        return out
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if name in staging_names or name in SKIP_DIRNAMES_COMMON:
            continue
        if name.startswith(".") and scope == "audiobook":
            continue
        if entry.is_file():
            out.append(
                CleanupCandidate(
                    path=str(entry),
                    kind="root_orphan_file",
                    reason="Loose file at library root (not part of Author/Title layout)",
                    size_bytes=_safe_size(entry),
                    scope=scope,
                )
            )
    return out


def _empty_dirs_under(
    root: Path, *, scope: Scope, staging_names: set[str]
) -> list[CleanupCandidate]:
    out: list[CleanupCandidate] = []
    if not root.is_dir():
        return out
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        try:
            rel_parts = p.resolve().relative_to(root.resolve()).parts
        except (OSError, ValueError):
            continue
        if any(part in staging_names for part in rel_parts):
            continue
        if any(part in SKIP_DIRNAMES_COMMON for part in rel_parts):
            continue
        try:
            if not any(p.iterdir()):
                out.append(
                    CleanupCandidate(
                        path=str(p),
                        kind="empty_dir",
                        reason="Empty folder under library root",
                        size_bytes=0,
                        scope=scope,
                    )
                )
        except OSError:
            continue
    return out


def classify_library_orphans(
    *,
    scopes: Iterable[Scope] | None = None,
    audiobook_root: Path | None = None,
    ebook_root: Path | None = None,
) -> CleanupPreview:
    """Build a dry-run preview of non-canonical leftovers."""
    wanted = list(scopes or ("audiobook", "ebook"))
    candidates: list[CleanupCandidate] = []
    protected: list[str] = []

    if "audiobook" in wanted:
        root = Path(audiobook_root or settings.audiobook_dir)
        protected.append(str(root))
        staging = set(unorganized_dirnames())
        book_dirs = list(iter_audiobook_book_dirs(root))
        for d in book_dirs:
            candidates.extend(classify_audiobook_dir(d))
        candidates.extend(_classify_hardlinked_m4b_dupes(book_dirs))
        candidates.extend(
            _root_level_orphans(root, scope="audiobook", staging_names=staging)
        )
        candidates.extend(
            _empty_dirs_under(root, scope="audiobook", staging_names=staging)
        )

    if "ebook" in wanted:
        root = Path(ebook_root or settings.ebook_dir)
        protected.append(str(root))
        staging = {ebook_staging_dirname()}
        for d in iter_ebook_book_dirs(root):
            candidates.extend(classify_ebook_dir(d))
        candidates.extend(
            _root_level_orphans(root, scope="ebook", staging_names=staging)
        )
        candidates.extend(
            _empty_dirs_under(root, scope="ebook", staging_names=staging)
        )

    seen: set[str] = set()
    unique: list[CleanupCandidate] = []
    for c in candidates:
        key = str(Path(c.path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    token_src = f"{time.time()}:{len(unique)}:{','.join(wanted)}"
    token = hashlib.sha256(token_src.encode()).hexdigest()[:32]
    preview = CleanupPreview(
        token=token,
        candidates=unique,
        protected_roots=protected,
        scopes=wanted,
    )
    _preview_cache[token] = {
        "created_at": preview.created_at,
        "paths": {c.path for c in unique},
        "scopes": wanted,
    }
    now = time.time()
    for k, v in list(_preview_cache.items()):
        if now - float(v.get("created_at") or 0) > _PREVIEW_TTL_SEC:
            _preview_cache.pop(k, None)
    return preview


def _assert_deletable(path: Path, *, scopes: list[str]) -> Path:
    """Refuse deletes outside library roots or inside staging."""
    resolved = path.resolve()
    allowed_roots: list[tuple[Path, set[str]]] = []
    if "audiobook" in scopes:
        allowed_roots.append(
            (Path(settings.audiobook_dir).resolve(), set(unorganized_dirnames()))
        )
    if "ebook" in scopes:
        allowed_roots.append(
            (Path(settings.ebook_dir).resolve(), {ebook_staging_dirname()})
        )

    for root, staging in allowed_roots:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if resolved == root:
            raise ValueError("Refusing to delete library root")
        if any(part in staging for part in rel.parts):
            raise ValueError(f"Refusing to delete staging path: {resolved}")
        return resolved
    raise ValueError(f"Path outside managed library roots: {resolved}")


def apply_cleanup(
    *,
    token: str,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    """Delete previously previewed orphan paths (subset allowed)."""
    cached = _preview_cache.get(token)
    if not cached:
        raise ValueError("Preview token expired or unknown — run preview again")
    if time.time() - float(cached.get("created_at") or 0) > _PREVIEW_TTL_SEC:
        _preview_cache.pop(token, None)
        raise ValueError("Preview token expired — run preview again")

    allowed: set[str] = set(cached.get("paths") or set())
    scopes = list(cached.get("scopes") or ["audiobook", "ebook"])
    selected = list(paths) if paths else sorted(allowed)

    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for raw in selected:
        if raw not in allowed:
            errors.append({"path": raw, "error": "Not in preview set"})
            continue
        try:
            target = _assert_deletable(Path(raw), scopes=scopes)
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
                deleted.append(str(target))
            elif target.is_dir():
                shutil.rmtree(target)
                deleted.append(str(target))
            else:
                errors.append({"path": raw, "error": "Path no longer exists"})
        except Exception as e:
            errors.append({"path": raw, "error": str(e)[:300]})

    _preview_cache.pop(token, None)
    return {
        "ok": len(errors) == 0,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "errors": errors,
    }
