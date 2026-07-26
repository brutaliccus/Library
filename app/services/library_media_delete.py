"""Path-safe deletion of on-disk library media."""

from __future__ import annotations

import shutil
from pathlib import Path

from app.services.ebook_pipeline import ebook_staging_dirname
from app.services.forge_pipeline import unorganized_dirnames

# Defaults for tests / import-time callers; prefer the getters below at runtime.
ABS_FORBIDDEN_DIRNAMES = frozenset({".unorganized", "_unorganized"})
EBOOK_FORBIDDEN_DIRNAMES = frozenset({"unorganized"})


def get_abs_forbidden_dirnames() -> frozenset[str]:
    return unorganized_dirnames()


def get_ebook_forbidden_dirnames() -> frozenset[str]:
    return frozenset({ebook_staging_dirname()})


def _assert_deletable_under(
    target: Path,
    library_root: Path,
    forbidden_dirnames: frozenset[str],
) -> Path:
    """Ensure target is a strict subdirectory of library_root and not protected."""
    root = library_root.resolve()
    resolved = target.resolve()
    if resolved == root:
        raise ValueError("Refusing to delete library root")
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Path is outside library root") from exc
    if not rel.parts:
        raise ValueError("Refusing to delete library root")
    for part in rel.parts:
        if part in forbidden_dirnames:
            raise ValueError(f"Refusing to delete protected directory: {part}")
    return resolved


def resolve_abs_book_dir(audiobook_dir: Path, library_item: dict) -> Path:
    rel = (library_item.get("relPath") or "").strip().strip("/" + chr(92))
    if not rel:
        path_str = (library_item.get("path") or "").strip()
        if path_str:
            return _assert_deletable_under(Path(path_str), audiobook_dir, get_abs_forbidden_dirnames())
        raise ValueError("Missing relPath on library item")
    if ".." in Path(rel).parts:
        raise ValueError("Path traversal is not allowed")
    book_dir = audiobook_dir / rel
    return _assert_deletable_under(book_dir, audiobook_dir, get_abs_forbidden_dirnames())


def resolve_ebook_book_dirs(ebook_dir: Path, file_paths: list[Path]) -> list[Path]:
    if not file_paths:
        raise ValueError("No local ebook files for series")
    root = ebook_dir.resolve()
    forbidden = get_ebook_forbidden_dirnames()
    parents: set[Path] = set()
    for raw in file_paths:
        p = Path(raw).resolve()
        parent = p.parent if p.is_file() or p.suffix else p
        parents.add(_assert_deletable_under(parent, root, forbidden))
    ordered = sorted(parents, key=lambda x: len(x.parts), reverse=True)
    out: list[Path] = []
    for candidate in ordered:
        if any(candidate != other and candidate.is_relative_to(other) for other in ordered if candidate != other):
            continue
        out.append(candidate)
    return out


def _prune_empty_parents(
    start: Path,
    library_root: Path,
    forbidden_dirnames: frozenset[str],
) -> None:
    root = library_root.resolve()
    cur = start.resolve()
    while cur != root:
        if not cur.is_dir():
            cur = cur.parent
            continue
        try:
            next(cur.iterdir())
            break
        except StopIteration:
            pass
        except OSError:
            break
        try:
            _assert_deletable_under(cur, root, forbidden_dirnames)
        except ValueError:
            break
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent


def delete_tree_under_library(
    target: Path,
    library_root: Path,
    forbidden_dirnames: frozenset[str],
) -> None:
    path = _assert_deletable_under(target, library_root, forbidden_dirnames)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink()
    else:
        return
    _prune_empty_parents(path.parent, library_root, forbidden_dirnames)
