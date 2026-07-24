"""Post-download LibraForge orchestration: metadata → M4B → re-apply → Folder Forge → ABS."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import DownloadRequest, User
from app.services import audiobookshelf, downloader, libraforge, push

logger = logging.getLogger(__name__)
settings = get_settings()

# Dot-directory so Audiobookshelf skips staging (ABS indexes `_unorganized`).
UNORGANIZED_DIRNAME = ".unorganized"
LEGACY_UNORGANIZED_DIRNAME = "_unorganized"
UNORGANIZED_DIRNAMES = frozenset({UNORGANIZED_DIRNAME, LEGACY_UNORGANIZED_DIRNAME})
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav", ".wma", ".aac", ".mp4"}
# Torrent / catalog noise that hurts Audible search when used as a title hint.
_TITLE_JUNK_RE = re.compile(
    r"\s*[\[(](?:complete\s+series|chapterized|full[-\s]?cast(?:\s+edition)?|"
    r"mp3|m4b|64\s*kbps|128\s*kbps|audiobook)[\])]|\s*[\[(]\d{3,4}p[\])]",
    re.IGNORECASE,
)
_SERIES_PACK_RE = re.compile(
    r"\b(?:complete\s+series|books?\s*\d+\s*[-–]\s*\d+|omnibus|box\s*set)\b",
    re.IGNORECASE,
)


def _pipeline():
    """Lazy import to avoid circular dependency with pipeline.py."""
    from app.services import pipeline as p
    return p

# Active forge statuses (cancel / resume)
FORGE_STATUSES = frozenset({
    "metadata_forge",
    "m4b_convert",
    "folder_forge",
    "finalizing",
    "organizing",  # legacy alias during transition
})

# Truly finished — quarantined is NOT terminal (admin may continue review).
PIPELINE_TERMINAL = frozenset({
    "completed",
    "failed",
    "cancelled",
    "admin_rejected",
})


def unorganized_root() -> Path:
    return Path(settings.audiobook_dir) / UNORGANIZED_DIRNAME


def ensure_unorganized_root() -> Path:
    """Create ``.unorganized`` (and a sibling ``.ignore``) for ABS-safe staging."""
    root = unorganized_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        ignore = root / ".ignore"
        if not ignore.exists():
            ignore.write_text("", encoding="utf-8")
    except OSError as e:
        logger.warning("Could not ensure unorganized staging root %s: %s", root, e)
    return root


def audiobook_staging_dir(request_id: int, title: str) -> Path:
    """Per-request landing folder under ``.unorganized`` (not final library layout)."""
    slug = downloader.sanitize_filename(title or "book")[:80] or "book"
    return ensure_unorganized_root() / f"req_{request_id}_{slug}"


def staging_path_for_libraforge(staging: Path) -> str:
    """Absolute POSIX-style path as seen inside Docker (/audiobooks/...)."""
    try:
        resolved = staging.resolve()
    except OSError:
        resolved = staging
    # Prefer path relative to configured audiobook_dir so LibraForge + Library share names.
    root = Path(settings.audiobook_dir).resolve()
    try:
        rel = resolved.relative_to(root)
        return str((Path(settings.audiobook_dir) / rel).as_posix())
    except ValueError:
        return str(resolved.as_posix())


def _collect_audio(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        f for f in folder.rglob("*")
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        and "-tmpfiles" not in f.parts
    )


def needs_m4b_conversion(folder: Path) -> bool:
    audio = _collect_audio(folder)
    if not audio:
        return False
    m4bs = [f for f in audio if f.suffix.lower() == ".m4b"]
    if len(m4bs) == 1 and len(audio) == 1:
        return False
    return True


def clean_catalog_title(title: str) -> str:
    """Strip torrent / pack noise so Metadata Forge gets a usable title hint."""
    t = (title or "").strip()
    if not t:
        return ""
    t = _TITLE_JUNK_RE.sub("", t)
    # "Book Title, Complete Series, Chapterized" → "Book Title"
    t = re.sub(
        r"\s*,\s*(?:complete\s+series|chapterized|full[-\s]?cast(?:\s+edition)?)\b.*$",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # "Book Title (The Series I)" — keep for Gunslinger-style; series pack phrases already removed
    t = re.sub(r"\s{2,}", " ", t).strip(" -–_|,")
    return t


def _audio_parent_dirs(folder: Path) -> list[Path]:
    """Unique parent directories that directly contain audio files."""
    parents: list[Path] = []
    seen: set[Path] = set()
    for audio in _collect_audio(folder):
        parent = audio.parent
        if parent in seen:
            continue
        seen.add(parent)
        parents.append(parent)
    return parents


def seed_staging_metadata_hints(
    staging: Path,
    *,
    title: str,
    author: str | None,
) -> None:
    """Write ABS-style metadata.json hints for single-book staging folders.

    LibraForge Metadata Forge derives Audible queries from local tags / folder
    names. Catalog title+author from DownloadRequest are much more reliable for
    typical one-book downloads with empty tags. Multi-book packs are left alone
    (folder names are better than a series-pack request title).
    """
    audio = _collect_audio(staging)
    if not audio:
        return
    parents = _audio_parent_dirs(staging)
    # One loose file, or one chapter-folder — not a multi-title pack.
    if len(parents) != 1:
        return
    raw_title = (title or "").strip()
    if raw_title and _SERIES_PACK_RE.search(raw_title):
        return

    hint_title = clean_catalog_title(raw_title) or raw_title
    hint_author = (author or "").strip()
    if not hint_title and not hint_author:
        return

    target_dir = parents[0]
    meta_path = target_dir / "metadata.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError):
            meta = {}

    changed = False
    if hint_title and not str(meta.get("title") or "").strip():
        meta["title"] = hint_title
        changed = True
    if hint_author and hint_author.lower() != "unknown":
        authors = meta.get("authors")
        if not isinstance(authors, list):
            authors = []
        if not any(str(a).strip() for a in authors):
            meta["authors"] = [hint_author]
            changed = True
        if not str(meta.get("author") or "").strip():
            meta["author"] = hint_author
            changed = True

    if not changed:
        return
    try:
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(
            "Seeded Metadata Forge hints in %s (title=%r author=%r)",
            meta_path,
            meta.get("title"),
            hint_author,
        )
    except OSError as e:
        logger.warning("Could not seed metadata.json in %s: %s", target_dir, e)


def _staging_library_roots() -> list[tuple[Path, tuple[str, ...]]]:
    """(library_root, staging_dirname_variants) for audiobook + ebook staging."""
    from app.services.ebook_pipeline import EBOOK_UNORGANIZED_DIRNAME

    return [
        (
            Path(settings.audiobook_dir).resolve(),
            (UNORGANIZED_DIRNAME, LEGACY_UNORGANIZED_DIRNAME),
        ),
        (
            Path(settings.ebook_dir).resolve(),
            (EBOOK_UNORGANIZED_DIRNAME,),
        ),
    ]


def all_staging_roots() -> list[Path]:
    """Resolved staging root dirs (audiobook ``.unorganized`` + ebook ``unorganized``)."""
    roots: list[Path] = []
    for lib_root, names in _staging_library_roots():
        for name in names:
            roots.append((lib_root / name).resolve())
    return roots


def resolve_staging_dir(staging_str: str) -> Path:
    """Resolve a request staging_path to a real directory under a staging root.

    Accepts POSIX Docker-style paths stored in the DB, e.g.
    ``/audiobooks/.unorganized/req_12_Title``, legacy ``_unorganized``, or
    ``/ebooks/unorganized/req_12_Title`` (and host ``/mnt/...`` remaps).
    Rejects anything outside configured audiobook/ebook staging trees.
    """
    raw = (staging_str or "").strip()
    if not raw:
        raise FileNotFoundError("Request has no staging_path")

    from app.services.ebook_pipeline import EBOOK_UNORGANIZED_DIRNAME

    lib_specs = _staging_library_roots()
    staging_roots = all_staging_roots()
    candidates: list[Path] = [Path(raw)]

    # Normalize Docker / host mount prefixes to paths under library roots.
    # Path.parts keeps the root as '/' or 'C:\\'; drop that for remapping.
    norm_parts = [x for x in Path(raw.replace("\\", "/")).parts if x not in ("/", "\\")]
    if norm_parts:
        mapped = list(norm_parts)
        if mapped[0].lower() == "mnt" and len(mapped) >= 2:
            # /mnt/Audiobooks/.unorganized/... or /mnt/eBooks/unorganized/...
            mapped = mapped[2:]
        elif mapped[0].lower() in {"audiobooks", "ebooks", "data"}:
            mapped = mapped[1:]
        if mapped:
            for lib_root, names in lib_specs:
                candidates.append(lib_root.joinpath(*mapped))
                # Rewrite audiobook legacy _unorganized ↔ .unorganized.
                for old, new in (
                    (LEGACY_UNORGANIZED_DIRNAME, UNORGANIZED_DIRNAME),
                    (UNORGANIZED_DIRNAME, LEGACY_UNORGANIZED_DIRNAME),
                ):
                    if old in mapped:
                        remapped = [new if p == old else p for p in mapped]
                        candidates.append(lib_root.joinpath(*remapped))
                for name in names:
                    if name in mapped:
                        idx = mapped.index(name)
                        candidates.append(lib_root.joinpath(*mapped[idx:]))
            # Bare req_* folder name under any staging root
            for root in staging_roots:
                candidates.append(root / mapped[-1])
            # Also try ebook unorganized if path only had audiobook-style parts
            if EBOOK_UNORGANIZED_DIRNAME in mapped or any(
                n in mapped for n in UNORGANIZED_DIRNAMES
            ):
                pass

    if not Path(raw).is_absolute():
        for lib_root, _names in lib_specs:
            candidates.append(lib_root / raw)

    seen: set[Path] = set()
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        under_staging = False
        for root in staging_roots:
            try:
                resolved.relative_to(root)
                under_staging = True
                break
            except ValueError:
                continue
        if not under_staging:
            continue
        if resolved.is_dir():
            return resolved

    raise FileNotFoundError(
        f"Staging folder missing or outside unorganized staging: {raw}"
    )


def safe_path_under_staging(staging: Path, relative: str) -> Path:
    """Resolve ``relative`` under staging; reject traversal / absolute paths."""
    rel = (relative or "").strip().replace("\\", "/")
    if not rel or rel in {".", "./"}:
        raise ValueError("Path is required")
    if rel.startswith("/") or rel.startswith("~"):
        raise ValueError("Absolute paths are not allowed")
    if ".." in Path(rel).parts:
        raise ValueError("Path traversal is not allowed")

    staging_res = staging.resolve()
    target = (staging_res / rel).resolve()
    try:
        target.relative_to(staging_res)
    except ValueError as e:
        raise ValueError("Path escapes staging folder") from e
    return target


def _entry_size(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def build_staging_tree(staging: Path, *, max_entries: int = 2000) -> dict[str, Any]:
    """Nested folder/file tree for the admin staging browser (relative paths only)."""
    staging_res = staging.resolve()
    count = 0
    truncated = False

    def walk(folder: Path) -> list[dict[str, Any]]:
        nonlocal count, truncated
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(folder.iterdir(), key=lambda c: (not c.is_dir(), c.name.casefold()))
        except OSError:
            return entries
        for child in children:
            if truncated:
                break
            # Skip obscure dotdirs; keep metadata sidecars (.m4b-tool-metadata.json).
            if child.name.startswith(".") and child.name not in {
                ".m4b-tool-metadata.json",
            }:
                continue
            if child.name == "-tmpfiles" or "-tmpfiles" in child.parts:
                continue
            if count >= max_entries:
                truncated = True
                break
            count += 1
            try:
                rel = child.relative_to(staging_res).as_posix()
            except ValueError:
                continue
            if child.is_dir():
                entries.append({
                    "name": child.name,
                    "path": rel,
                    "type": "dir",
                    "size": None,
                    "ext": None,
                    "children": walk(child),
                })
            elif child.is_file():
                ext = child.suffix.lower() or None
                entries.append({
                    "name": child.name,
                    "path": rel,
                    "type": "file",
                    "size": _entry_size(child),
                    "ext": ext,
                    "children": None,
                })
        return entries

    return {
        "staging_path": staging_path_for_libraforge(staging_res),
        "root_name": staging_res.name,
        "entries": walk(staging_res),
        "entry_count": count,
        "truncated": truncated,
    }


def delete_staging_entry(staging: Path, relative: str) -> dict[str, Any]:
    """Delete a file or directory (recursive) under staging. Path-traversal safe.

    The staging root itself cannot be removed — only nested entries — so the
    request keeps a valid quarantine folder.
    """
    staging_res = staging.resolve()
    target = safe_path_under_staging(staging, relative)
    if target == staging_res:
        raise ValueError("Cannot delete the staging root")
    if not target.exists():
        raise FileNotFoundError(f"Not found: {relative}")
    if target.is_dir():
        shutil.rmtree(target)
        return {"ok": True, "deleted": relative, "type": "dir"}
    if not target.is_file():
        raise ValueError("Not a file or directory")
    target.unlink()
    # Prune empty parent dirs up to (but not including) staging root
    parent = target.parent
    while parent != staging_res and parent.is_relative_to(staging_res):
        try:
            next(parent.iterdir())
            break
        except StopIteration:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        except OSError:
            break
    return {"ok": True, "deleted": relative, "type": "file"}


def _remove_source_audio_after_m4b(staging: Path) -> int:
    """After a successful M4B merge, remove non-.m4b audio left in staging.

    Keeps a single book for Metadata Forge re-apply and Folder Forge. Does not
    touch files outside ``staging``. Returns number of files removed.
    """
    audio = _collect_audio(staging)
    m4bs = [f for f in audio if f.suffix.lower() == ".m4b"]
    if len(m4bs) < 1:
        return 0
    removed = 0
    for path in audio:
        if path.suffix.lower() == ".m4b":
            continue
        try:
            path.unlink()
            removed += 1
            logger.info("Removed source audio after M4B: %s", path)
        except OSError as e:
            logger.warning("Could not remove source audio %s: %s", path, e)
    return removed


def cover_url_from_staging(staging: Path) -> str | None:
    """Best-effort cover URL from metadata.json / libraforge.json for M4B --cover."""
    if not staging.is_dir():
        return None
    for meta_path in staging.rglob("metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        for key in ("cover_url", "cover", "image_url"):
            val = str(meta.get(key) or "").strip()
            if val.startswith("http"):
                return val
    for marker_path in staging.rglob("libraforge.json"):
        try:
            data = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for block_key in ("marker", "sidecar", "book", "audible", "backup"):
            block = data.get(block_key)
            if not isinstance(block, dict):
                continue
            for key in ("cover_url", "cover", "image_url"):
                val = str(block.get(key) or "").strip()
                if val.startswith("http"):
                    return val
            applied = block.get("applied_tags") if isinstance(block.get("applied_tags"), dict) else {}
            val = str(applied.get("cover_url") or applied.get("cover") or "").strip()
            if val.startswith("http"):
                return val
        val = str(data.get("cover_url") or "").strip()
        if val.startswith("http"):
            return val
    return None


async def _apply_metadata_forge(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    phase_detail: str = "Matching metadata via LibraForge…",
) -> bool:
    """Run Metadata Forge apply (overwrite + replace_cover). Returns True if applied.

    On failure / no write evidence, quarantines and returns False.
    """
    p = _pipeline()
    lf_path = staging_path_for_libraforge(staging)

    async def _abort_if_cancelled() -> bool:
        return await p._is_cancelled(request_id)

    async with async_session() as db:
        await p._update_status(db, request_id, "metadata_forge", phase_detail)

    async def _on_meta(state: dict[str, Any]) -> None:
        await _forge_progress(request_id, user_id, "metadata_forge", state)

    try:
        run_id = await libraforge.start_metadata_run(
            lf_path,
            apply=True,
            min_score=settings.libraforge_min_score,
            write_mode="overwrite",
            cover_if_missing=False,
            replace_cover=True,
        )
        await _persist_staging(request_id, staging, run_id=run_id)
        report = await libraforge.wait_for_run(
            run_id,
            poll_seconds=3.0,
            timeout_seconds=settings.libraforge_metadata_timeout,
            on_progress=_on_meta,
            should_abort=_abort_if_cancelled,
        )
    except libraforge.LibraForgeError as e:
        if "cancelled" in str(e).lower():
            return False
        await _set_quarantine(
            request_id,
            f"LibraForge Metadata Forge unavailable or failed: {e}",
            staging,
        )
        return False

    if libraforge.run_failed(report):
        err = report.get("error") or report.get("status") or "Metadata Forge failed"
        await _set_quarantine(request_id, str(err)[:500], staging)
        return False

    if not libraforge.metadata_auto_applied(report):
        await _set_quarantine(
            request_id,
            libraforge.quarantine_reason_from_report(report),
            staging,
        )
        return False

    if not staging_has_applied_metadata(staging):
        await _set_quarantine(
            request_id,
            (
                "Metadata Forge reported a write, but no applied libraforge.json / "
                "ASIN metadata.json was found in staging. Match may not have been "
                "persisted (permissions or apply race)."
            ),
            staging,
        )
        return False
    return True


def staging_has_applied_metadata(staging: Path) -> bool:
    """True when staging contains LibraForge apply markers / ABS metadata with ASIN.

    Used as a second gate after the API report claims a write, so we do not
    continue to M4B/Folder Forge on a match-only / dry-run result.
    """
    if not staging.is_dir():
        return False
    for marker in staging.rglob("libraforge.json"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        m = data.get("marker") if isinstance(data.get("marker"), dict) else data
        if m.get("applied") is True or m.get("manually_applied") is True:
            return True
        backup = data.get("backup") if isinstance(data.get("backup"), dict) else {}
        applied_tags = backup.get("applied_tags") if isinstance(backup.get("applied_tags"), dict) else {}
        if applied_tags.get("asin") or applied_tags.get("title"):
            return True
    for meta_path in staging.rglob("metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        asin = str(meta.get("asin") or "").strip()
        if asin:
            return True
    return False


async def _persist_staging(request_id: int, staging: Path, run_id: str | None = None) -> None:
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            return
        req.staging_path = staging_path_for_libraforge(staging)
        if run_id is not None:
            req.libraforge_run_id = run_id
        await db.commit()


async def _set_quarantine(request_id: int, reason: str, staging: Path) -> None:
    p = _pipeline()
    if await p._is_cancelled(request_id):
        return
    async with async_session() as db:
        review_url = libraforge.public_manual_review_url()
        detail = reason[:500]
        if review_url:
            detail = f"{reason[:350]} · Review: {review_url}"
        await p._update_status(db, request_id, "quarantined", detail)
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if req:
            if req.status == "cancelled":
                return
            req.staging_path = staging_path_for_libraforge(staging)
            req.quarantine_reason = reason[:500]
            req.progress_percent = None
            req.progress_bytes = None
            req.progress_total_bytes = None
            req.progress_speed_bps = None
            await db.commit()
            user_result = await db.execute(select(User).where(User.id == req.user_id))
            user = user_result.scalar_one_or_none()
            username = user.username if user else "Unknown"
            try:
                await push.notify_admins(db, {
                    "type": "download_quarantined",
                    "title": "Request quarantined — admin review",
                    "body": f"{req.title} (by {username}): {reason[:180]}",
                    "url": "/admin?tab=requests",
                })
            except Exception:
                logger.warning("Quarantine admin push failed", exc_info=True)


async def _forge_progress(
    request_id: int,
    user_id: int,
    status: str,
    state: dict[str, Any],
) -> None:
    p = _pipeline()
    label = state.get("phase_label") or state.get("phase") or status
    detail = state.get("phase_detail") or state.get("current_file") or str(label)
    pct = state.get("percent")
    if pct is None and state.get("total"):
        try:
            cur = float(state.get("current") or 0)
            total = float(state["total"])
            if total > 0:
                pct = min(100.0, cur / total * 100)
        except (TypeError, ValueError):
            pct = None
    await p._report_progress(
        request_id,
        user_id,
        status,
        str(detail)[:400],
        progress_percent=float(pct) if pct is not None else None,
    )


async def run_forge_after_download(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    title: str,
    author: str | None = None,
    resume_from: str | None = None,
) -> None:
    """Run Metadata Forge → M4B → post-M4B Metadata Forge → Folder Forge → ABS.

    After a successful M4B convert, Metadata Forge is re-applied (overwrite +
    replace_cover) because m4b-tool re-encode does not preserve embedded covers
    and ``enforce_m4b_output_metadata`` only writes text tags.

    ``resume_from``: None (full), ``m4b``, ``folder``, or ``finalize``.
    Used after admin Manual Review in LibraForge.
    """
    p = _pipeline()
    staging.mkdir(parents=True, exist_ok=True)
    lf_path = staging_path_for_libraforge(staging)
    await _persist_staging(request_id, staging)

    start_step = resume_from or "metadata"
    if await p._is_cancelled(request_id):
        return

    async def _abort_if_cancelled() -> bool:
        return await p._is_cancelled(request_id)

    # --- Metadata Forge ---
    if start_step == "metadata":
        # Prefer catalog title/author when tags are empty (single-book only).
        if not author:
            async with async_session() as db:
                result = await db.execute(
                    select(DownloadRequest).where(DownloadRequest.id == request_id)
                )
                req_row = result.scalar_one_or_none()
                if req_row:
                    author = req_row.author
                    if not title:
                        title = req_row.title
        seed_staging_metadata_hints(staging, title=title, author=author)

        # Score ≥ min_score means match identity is trusted — not that the
        # torrent's existing tags/cover are correct. Force full overwrite.
        applied = await _apply_metadata_forge(
            request_id,
            staging=staging,
            user_id=user_id,
            phase_detail="Matching metadata via LibraForge…",
        )
        if not applied:
            return
        start_step = "m4b"

    if await p._is_cancelled(request_id):
        return

    # --- M4B (Pi LibraForge) ---
    if start_step == "m4b":
        m4b_produced_new_file = False
        if needs_m4b_conversion(staging):
            async with async_session() as db:
                await p._update_status(db, request_id, "m4b_convert", "Converting to M4B on Pi…")

            async def _on_m4b(state: dict[str, Any]) -> None:
                await _forge_progress(request_id, user_id, "m4b_convert", state)

            try:
                loaded = await libraforge.m4b_load(lf_path)
                meta = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}
                if not isinstance(meta, dict):
                    meta = {}
                # m4b-tool re-encodes; embedded covers from Metadata Forge are NOT
                # copied unless cover_url is passed for --cover. Pull from sidecar.
                if not str(meta.get("cover_url") or "").strip():
                    cover = cover_url_from_staging(staging)
                    if cover:
                        meta = {**meta, "cover_url": cover}
                if not meta.get("title"):
                    meta = {**meta, "title": title}
                output_path = (
                    loaded.get("output_path")
                    or str((staging / f"{downloader.sanitize_filename(title) or staging.name}.m4b").as_posix())
                )
                # Prefer converting the staging folder as a whole
                input_path = lf_path
                run_id = await libraforge.start_m4b_run(
                    input_path,
                    str(output_path),
                    metadata=meta,
                    jobs=settings.libraforge_m4b_jobs,
                )
                await _persist_staging(request_id, staging, run_id=run_id)
                report = await libraforge.wait_for_run(
                    run_id,
                    poll_seconds=5.0,
                    timeout_seconds=settings.libraforge_m4b_timeout,
                    on_progress=_on_m4b,
                    should_abort=_abort_if_cancelled,
                )
                if libraforge.run_failed(report):
                    # Soft-fail: keep going to Folder Forge with source audio
                    logger.warning(
                        "M4B conversion failed for request %s — continuing with source files: %s",
                        request_id,
                        report.get("error") or report.get("status"),
                    )
                    async with async_session() as db:
                        await p._update_status(
                            db,
                            request_id,
                            "m4b_convert",
                            "M4B conversion failed on Pi; organizing source audio…",
                        )
                else:
                    m4b_produced_new_file = True
                    # Drop source parts so post-M4B Metadata Forge / ABS see one book.
                    _remove_source_audio_after_m4b(staging)
            except libraforge.LibraForgeError as e:
                if "cancelled" in str(e).lower():
                    return
                # Pi may be underpowered — note tradeoff, don't hard-fail the request
                logger.warning(
                    "M4B on Pi failed for request %s (%s). "
                    "Heavy jobs can use Windows LibraForge :5057 manually.",
                    request_id,
                    e,
                )
                async with async_session() as db:
                    await p._update_status(
                        db,
                        request_id,
                        "m4b_convert",
                        f"M4B skipped (Pi error): {e}"[:400],
                    )
        else:
            async with async_session() as db:
                await p._update_status(db, request_id, "m4b_convert", "Already a single M4B — skipping convert")
                await p._report_progress(
                    request_id, user_id, "m4b_convert", "Already a single M4B — skipping convert",
                    progress_percent=100.0,
                )

        # M4B re-encode drops embedded covers / can leave stale tags on the new
        # file. Re-apply Metadata Forge (overwrite + replace_cover) onto the
        # post-convert .m4b before Folder Forge moves it into the library.
        if m4b_produced_new_file:
            if await p._is_cancelled(request_id):
                return
            logger.info(
                "Re-applying Metadata Forge after M4B for request %s (cover/tags persist)",
                request_id,
            )
            reapplied = await _apply_metadata_forge(
                request_id,
                staging=staging,
                user_id=user_id,
                phase_detail="Re-applying metadata + cover after M4B…",
            )
            if not reapplied:
                return

        start_step = "folder"

    if await p._is_cancelled(request_id):
        return

    # --- Folder Forge ---
    if start_step == "folder":
        async with async_session() as db:
            await p._update_status(
                db,
                request_id,
                "folder_forge",
                "Organizing into library folders…",
            )

        async def _on_folder(state: dict[str, Any]) -> None:
            await _forge_progress(request_id, user_id, "folder_forge", state)

        try:
            run_id = await libraforge.start_organizer_run(
                lf_path,
                destination_root=settings.audiobook_dir,
                apply=True,
                naming_template=settings.libraforge_naming_template,
            )
            await _persist_staging(request_id, staging, run_id=run_id)
            report = await libraforge.wait_for_run(
                run_id,
                poll_seconds=3.0,
                timeout_seconds=settings.libraforge_organizer_timeout,
                on_progress=_on_folder,
                should_abort=_abort_if_cancelled,
            )
            if libraforge.run_failed(report):
                raise libraforge.LibraForgeError(
                    str(report.get("error") or report.get("status") or "Folder Forge failed")
                )
        except libraforge.LibraForgeError as e:
            if "cancelled" in str(e).lower():
                return
            raise RuntimeError(f"Folder Forge failed: {e}") from e

        # Folder Forge reporting success with zero moves while audio still sits
        # in staging means metadata was never applied (or tags are unusable).
        # Do not mark the request completed — quarantine for admin review.
        leftover_audio = _collect_audio(staging)
        if leftover_audio and not libraforge.organizer_moved_files(report):
            await _set_quarantine(
                request_id,
                (
                    "Folder Forge made no library moves while audio remains in staging "
                    f"({len(leftover_audio)} file(s)). Metadata was likely not applied — "
                    "use LibraForge Manual Review, then Continue pipeline."
                ),
                staging,
            )
            return

        # Clean empty staging dir if Folder Forge left it
        try:
            if staging.is_dir() and not any(staging.iterdir()):
                staging.rmdir()
        except OSError:
            pass
        start_step = "finalize"

    if await p._is_cancelled(request_id):
        return

    # --- Finalize (ABS) ---
    import asyncio

    async with async_session() as db:
        await p._update_status(db, request_id, "finalizing", "Scanning Audiobookshelf…")

    try:
        await audiobookshelf.scan_library()
        await asyncio.sleep(5)
        await audiobookshelf.remove_items_with_issues()
    except Exception as e:
        logger.warning("ABS scan after forge failed (non-fatal): %s", e)
        try:
            async with async_session() as db:
                await push.notify_admins(db, {
                    "type": "error",
                    "title": "Library Scan Failed",
                    "body": f"ABS scan failed after forging {title}: {e}",
                    "url": "/admin?tab=requests",
                })
        except Exception:
            pass

    audiobookshelf.invalidate_cache()

    async with async_session() as db:
        await p._update_status(db, request_id, "completed", "Ready in Audiobookshelf")
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            return
        user_result = await db.execute(select(User).where(User.id == req.user_id))
        user = user_result.scalar_one_or_none()
        username = user.username if user else "Unknown"
        try:
            await push.notify_admins(db, {
                "type": "download_complete",
                "title": "Download Complete",
                "body": f"{title} is now in the library (requested by {username})",
                "url": "/admin?tab=requests",
            })
        except Exception as e:
            logger.warning("Admin push notification failed (non-fatal): %s", e)
        try:
            await push.notify_download_complete(req.user_id, title, "Audiobookshelf", db)
        except Exception as e:
            logger.warning("Push notification failed (non-fatal): %s", e)


def delete_request_staging_tree(
    request_id: int,
    staging_str: str | None,
) -> list[Path]:
    """Recursively delete this request's staging dirs under staging roots only.

    Covers audiobook ``.unorganized`` / legacy ``_unorganized`` and ebook
    ``unorganized``. Resolves Docker-style ``staging_path`` via
    ``resolve_staging_dir``, and also removes any ``req_{id}_*`` leftovers.
    Never deletes outside those staging trees.
    """
    staging_roots = all_staging_roots()
    to_delete: dict[Path, None] = {}

    raw = (staging_str or "").strip()
    if raw:
        try:
            resolved = resolve_staging_dir(raw)
            to_delete[resolved.resolve()] = None
        except FileNotFoundError:
            logger.debug(
                "Reject staging resolve skipped for request %s (%s)",
                request_id,
                raw,
            )

    # Catch orphaned req_{id}_* trees even if staging_path was missing/stale.
    prefix = f"req_{request_id}_"
    for unorganized in staging_roots:
        try:
            if not unorganized.is_dir():
                continue
            for child in unorganized.iterdir():
                if not child.is_dir():
                    continue
                name = child.name
                if name == f"req_{request_id}" or name.startswith(prefix):
                    try:
                        to_delete[child.resolve()] = None
                    except OSError:
                        continue
        except OSError as e:
            logger.warning("Could not list unorganized root for reject cleanup: %s", e)

    deleted: list[Path] = []
    for path in to_delete:
        under_staging = False
        for root in staging_roots:
            try:
                path.relative_to(root)
                under_staging = True
                break
            except ValueError:
                continue
        if not under_staging:
            logger.warning("Refusing to delete path outside staging roots: %s", path)
            continue
        if not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
            deleted.append(path)
            logger.info("Deleted quarantine staging %s", path)
        except OSError as e:
            logger.warning("Could not delete staging %s: %s", path, e)
    return deleted


async def reject_quarantined_request(
    request_id: int,
    *,
    delete_files: bool = True,
    reason: str = "Rejected by admin",
) -> DownloadRequest:
    """Mark request admin_rejected, notify user, optionally delete staging files."""
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        if req.status not in ("quarantined", "failed", "metadata_forge"):
            raise ValueError(f"Cannot reject request in status '{req.status}'")

        staging_str = (req.staging_path or "").strip()
        forge_run_id = (getattr(req, "libraforge_run_id", None) or "").strip() or None
        title = req.title
        user_id = req.user_id

        detail = reason[:500]
        await _pipeline()._update_status(db, request_id, "admin_rejected", detail)
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if req:
            req.quarantine_reason = detail
            await db.commit()

        try:
            await push.send_push_to_user(
                db,
                user_id,
                {
                    "type": "download_failed",
                    "title": f"{title} was rejected",
                    "body": detail,
                    "url": "/requests",
                },
            )
        except Exception:
            logger.warning("User reject push failed", exc_info=True)

    if forge_run_id:
        try:
            await libraforge.cancel_run(forge_run_id)
        except Exception:
            logger.debug(
                "LibraForge cancel_run for rejected request %s failed",
                request_id,
                exc_info=True,
            )

    if delete_files:
        delete_request_staging_tree(request_id, staging_str or None)

    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        return result.scalar_one()


async def continue_after_manual_review(request_id: int) -> None:
    """Resume forge pipeline after admin applied metadata in LibraForge Manual Review.

    The admin continue endpoint normally flips status to ``m4b_convert`` (and
    clears quarantine) before scheduling this task so UIs update immediately.
    ``m4b_convert`` is therefore an accepted starting status here.
    """
    p = _pipeline()
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        if req.status not in ("quarantined", "metadata_forge", "m4b_convert"):
            raise ValueError(f"Cannot continue request in status '{req.status}'")
        staging_str = (req.staging_path or "").strip()
        if not staging_str:
            raise ValueError("Request has no staging_path")
        staging = Path(staging_str)
        if not staging.is_dir():
            # try resolving under audiobook_dir
            alt = Path(settings.audiobook_dir) / Path(staging_str).name
            if alt.is_dir():
                staging = alt
            else:
                raise FileNotFoundError(f"Staging folder missing: {staging_str}")
        user_id = req.user_id
        title = req.title
        author = req.author
        if req.quarantine_reason is not None:
            req.quarantine_reason = None
            await db.commit()
        # If the HTTP handler did not already leave quarantine, do it now (WS).
        if req.status in ("quarantined", "metadata_forge"):
            await p._update_status(
                db,
                request_id,
                "m4b_convert",
                "Resuming after manual review…",
            )

    await run_forge_after_download(
        request_id,
        staging=staging,
        user_id=user_id,
        title=title,
        author=author,
        resume_from="m4b",
    )
