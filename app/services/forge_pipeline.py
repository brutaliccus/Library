"""Post-download LibraForge orchestration: metadata → M4B → re-apply → Chapter Forge → Folder Forge → ABS."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import DownloadRequest, User
from app.services import audiobookshelf, chapter_embed, downloader, libraforge, m4b_queue, push

logger = logging.getLogger(__name__)
settings = get_settings()

# Real Audible ASINs (B0… / 10-digit). Ignore scan_cache sentinels like HAS_ASIN.
_ASIN_RE = re.compile(r"^(?:B[\dA-Z]{9}|\d{10})$", re.IGNORECASE)
_ASIN_SENTINELS = frozenset({"", "HAS_ASIN", "NOREALASIN", "NONE", "NULL", "N/A"})
# Same filename token LibraForge ``_FILENAME_ASIN_RE`` uses for owned-ASIN scans.
_FILENAME_ASIN_RE = re.compile(r"\[(?:ASIN\.)?([Bb]0[A-Z0-9]{8})\]", re.IGNORECASE)
# MP4 freeform keys LibraForge ``_ASIN_TAG_KEYS`` / ``_read_asin_from_audio`` probe.
_ASIN_TAG_KEYS = (
    "----:com.apple.iTunes:asin",
    "----:com.pilabor.tone:AUDIBLE_ASIN",
    "----:com.apple.iTunes:ASIN",
)

# Defaults — prefer settings.audiobook_staging_* (Admin Config → Storage / Paths).
UNORGANIZED_DIRNAME = ".unorganized"
LEGACY_UNORGANIZED_DIRNAME = "_unorganized"
UNORGANIZED_DIRNAMES = frozenset({UNORGANIZED_DIRNAME, LEGACY_UNORGANIZED_DIRNAME})


def audiobook_staging_dirname() -> str:
    name = (getattr(settings, "audiobook_staging_dirname", None) or UNORGANIZED_DIRNAME).strip()
    return name or UNORGANIZED_DIRNAME


def audiobook_staging_legacy_dirname() -> str:
    name = (
        getattr(settings, "audiobook_staging_legacy_dirname", None) or LEGACY_UNORGANIZED_DIRNAME
    ).strip()
    return name or LEGACY_UNORGANIZED_DIRNAME


def unorganized_dirnames() -> frozenset[str]:
    """Active + legacy audiobook staging folder names (config-aware)."""
    return frozenset({audiobook_staging_dirname(), audiobook_staging_legacy_dirname()})
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav", ".wma", ".aac", ".mp4"}
# Sibling folders that are alternate encodes of the same book (not separate titles).
_FORMAT_DIR_NAMES = frozenset({
    "mp3", "m4a", "m4b", "aac", "flac", "ogg", "opus", "wma", "wav", "audio", "audiobook",
})
# Prefer higher-quality / M4B-friendly containers when a torrent ships dual formats.
_FORMAT_EXT_RANK = {
    ".m4a": 4,
    ".aac": 4,
    ".mp4": 3,
    ".flac": 3,
    ".ogg": 2,
    ".opus": 2,
    ".mp3": 1,
    ".wma": 1,
    ".wav": 1,
}
# Torrent / catalog noise that hurts Audible search when used as a title hint.
_TITLE_JUNK_RE = re.compile(
    r"\s*[\[(](?:complete\s+series|chapterized|full[-\s]?cast(?:\s+edition)?|"
    r"graphic\s*audio|dramatized(?:\s+adaptation)?|mp3|m4b|64\s*kbps|"
    r"128\s*kbps|audiobook)[\])]|\s*[\[(]\d{3,4}p[\])]|\s*\(req[_\s-]?\d+\)",
    re.IGNORECASE,
)
_REQ_PREFIX_RE = re.compile(r"^req[_\s-]?\d+[_\s-]+", re.IGNORECASE)
_SERIES_BOOKNUM_PARENS_RE = re.compile(
    r"\s*[\[(][^)\]]*#\s*\d+(?:\.\d+)?[^)\]]*[\])]",
    re.IGNORECASE,
)
_TRAILING_EDITION_NOISE_RE = re.compile(
    r"\s*(?:graphic\s*audio|full[-\s]?cast(?:\s+edition)?|dramatized(?:\s+adaptation)?|"
    r"unabridged|chapterized)\s*$",
    re.IGNORECASE,
)
# "Mistborn 6 - The Bands of Mourning" / "Series 03 - Title" → keep the title part.
_SERIES_NUMBER_DASH_TITLE_RE = re.compile(
    r"^.+?\s+\d+(?:\.\d+)?\s*[-–:]\s+(.+)$",
)
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
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
    "chapter_forge",
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
    return Path(settings.audiobook_dir) / audiobook_staging_dirname()


def ensure_unorganized_root() -> Path:
    """Create audiobook staging root (and a sibling ``.ignore``) for ABS-safe staging."""
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
    """Per-request landing folder under audiobook staging (not final library layout)."""
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
    """Strip torrent / pack / staging noise so Metadata Forge gets a usable title hint.

    Keeps leading articles (``The``) for display and ABS tags. Prefer
    :func:`clean_search_title` for Audible/manual-review query strings.
    """
    t = (title or "").strip()
    if not t:
        return ""
    t = t.replace("_", " ")
    t = _REQ_PREFIX_RE.sub("", t)
    t = _TITLE_JUNK_RE.sub("", t)
    t = _SERIES_BOOKNUM_PARENS_RE.sub("", t)
    t = _TRAILING_EDITION_NOISE_RE.sub("", t)
    # "Book Title, Complete Series, Chapterized" → "Book Title"
    t = re.sub(
        r"\s*,\s*(?:complete\s+series|chapterized|full[-\s]?cast(?:\s+edition)?|"
        r"graphic\s*audio)\b.*$",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # "Mistborn 6 - The Bands of Mourning" → "The Bands of Mourning"
    m = _SERIES_NUMBER_DASH_TITLE_RE.match(t.strip())
    if m and len(m.group(1).split()) >= 2:
        t = m.group(1)
    # "Book Title (The Series I)" — keep for Gunslinger-style; series pack phrases already removed
    t = re.sub(r"\s{2,}", " ", t).strip(" -–_|,")
    return t


def clean_search_title(title: str) -> str:
    """Minimal title tokens for metadata search (no author / book # / articles)."""
    t = clean_catalog_title(title)
    if not t:
        return ""
    # Drop leading articles — they clutter Audible/ABS queries.
    t = _LEADING_ARTICLE_RE.sub("", t).strip()
    # Subtitles after colon rarely help identity search.
    if ":" in t:
        head, tail = t.split(":", 1)
        if len(head.split()) >= 2:
            t = head.strip()
        elif len(tail.split()) >= 2:
            t = tail.strip()
    # Trailing bare book numbers ("Title 5") — keep sole "1984".
    parts = t.split()
    while len(parts) > 1 and re.fullmatch(r"\d+(?:\.\d+)?", parts[-1] or ""):
        parts.pop()
    # "Book N" / "#N" tokens anywhere.
    parts = [
        p
        for p in parts
        if not re.fullmatch(r"(?:book|#)\d+(?:\.\d+)?", p or "", re.IGNORECASE)
        and not re.fullmatch(r"#", p or "")
    ]
    t = " ".join(parts).strip(" -–_|,.")
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


def _m4b_source_dir_score(folder: Path) -> tuple[int, int, int]:
    """Rank a folder for LibraForge M4B input (higher is better)."""
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    ] if folder.is_dir() else []
    ext_rank = 0
    for f in files:
        ext_rank = max(ext_rank, _FORMAT_EXT_RANK.get(f.suffix.lower(), 0))
    name_bonus = 1 if folder.name.lower() in _FORMAT_DIR_NAMES else 0
    return (ext_rank, name_bonus, len(files))


def m4b_source_dirs(folder: Path) -> list[Path]:
    """Directories to pass to LibraForge M4B (non-recursive file discovery).

    LibraForge ``resolve_m4b_input_files`` only scans *immediate* children of
    ``input_path`` (or sidecar ``chapter_files``). Staging trees often nest
    parts under ``mp3/``, ``AAC/``, etc. — pointing at the staging root then
    fails with ``No source audio files remain after excluding the output file``.

    - Flat audio in ``folder`` → ``[folder]``
    - Sibling format variants (``mp3`` + ``AAC``) → pick the best one
    - Multiple book folders → one entry per parent that still needs convert
    """
    parents = _audio_parent_dirs(folder)
    if not parents:
        return []
    if len(parents) == 1:
        return parents

    format_dirs = [p for p in parents if p.name.lower() in _FORMAT_DIR_NAMES]
    if format_dirs and len(format_dirs) == len(parents):
        best = max(format_dirs, key=_m4b_source_dir_score)
        return [best]

    return [p for p in parents if needs_m4b_conversion(p)]


def seed_staging_metadata_hints(
    staging: Path,
    *,
    title: str,
    author: str | None,
    asin: str | None = None,
    series: str | None = None,
    sequence: str | None = None,
    force: bool = False,
) -> None:
    """Write ABS-style metadata.json hints for single-book staging folders.

    LibraForge Metadata Forge derives Audible queries from local tags / folder
    names. Catalog title+author from DownloadRequest are much more reliable for
    typical one-book downloads with empty tags. Multi-book packs are left alone
    (folder names are better than a series-pack request title).

    ``force=True`` overwrites existing title/author/asin/series (used by LLM
    assist after a failed Metadata Forge pass).
    """
    audio = _collect_audio(staging)
    if not audio:
        return
    parents = _audio_parent_dirs(staging)
    # One loose file, or one chapter-folder — not a multi-title pack.
    if len(parents) != 1:
        return
    raw_title = (title or "").strip()
    if not force and raw_title and _SERIES_PACK_RE.search(raw_title):
        return

    hint_title = clean_catalog_title(raw_title) or raw_title
    hint_author = (author or "").strip()
    hint_asin = normalize_asin(asin) if asin else ""
    hint_series = (series or "").strip()
    hint_sequence = str(sequence or "").strip()
    # Store as "Series #N" so ABS/LibraForge keep the index across syncs.
    if hint_series and hint_sequence and "#" not in hint_series:
        hint_series = f"{hint_series} #{hint_sequence}"
    if not hint_title and not hint_author and not hint_asin:
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
    if hint_title and (force or not str(meta.get("title") or "").strip()):
        if str(meta.get("title") or "").strip() != hint_title:
            meta["title"] = hint_title
            changed = True
    if hint_author and hint_author.lower() != "unknown":
        authors = meta.get("authors")
        if not isinstance(authors, list):
            authors = []
        if force or not any(str(a).strip() for a in authors):
            if authors != [hint_author]:
                meta["authors"] = [hint_author]
                changed = True
        if force or not str(meta.get("author") or "").strip():
            if str(meta.get("author") or "").strip() != hint_author:
                meta["author"] = hint_author
                changed = True
    if hint_asin and (force or not normalize_asin(meta.get("asin"))):
        if str(meta.get("asin") or "").strip().upper() != hint_asin:
            meta["asin"] = hint_asin
            changed = True
    if hint_series and (force or not str(meta.get("series") or "").strip()):
        if str(meta.get("series") or "").strip() != hint_series:
            meta["series"] = hint_series
            changed = True

    if not changed:
        return
    try:
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(
            "Seeded Metadata Forge hints in %s (title=%r author=%r asin=%r force=%s)",
            meta_path,
            meta.get("title"),
            hint_author,
            hint_asin or "",
            force,
        )
    except OSError as e:
        logger.warning("Could not seed metadata.json in %s: %s", target_dir, e)


def _reseed_staging_hints_from_applied(
    staging: Path,
    *,
    title: str = "",
    author: str | None = None,
) -> None:
    """After M4B, force-seed metadata.json from the best applied marker / ABS file.

    New .m4b files often lack the manually_applied marker that lived next to the
    source mp3. Seeding author/ASIN here lets LibraForge rematch with catalog
    identity instead of narrator-as-author tags.
    """
    hint_title = ""
    hint_author = ""
    hint_asin = ""
    hint_series = ""

    for marker in list(staging.rglob("libraforge.json")) + list(
        staging.rglob("*.libraforge.json")
    ):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        m = data.get("marker") if isinstance(data.get("marker"), dict) else {}
        audible = m.get("audible") if isinstance(m.get("audible"), dict) else {}
        book = data.get("book") if isinstance(data.get("book"), dict) else {}
        if not book and isinstance(data.get("sidecar"), dict):
            book = data["sidecar"].get("book") if isinstance(data["sidecar"].get("book"), dict) else {}
        for source in (audible, book, m, data):
            if not isinstance(source, dict):
                continue
            if not hint_title:
                hint_title = str(
                    source.get("chosen_title")
                    or source.get("title")
                    or ""
                ).strip()
            if not hint_author:
                hint_author = str(source.get("author") or "").strip()
            if not hint_asin:
                hint_asin = normalize_asin(source.get("asin"))
            if not hint_series:
                hint_series = str(source.get("series") or "").strip()
        if hint_asin or (hint_title and hint_author):
            break

    if not hint_asin or not hint_title:
        for meta_path in staging.rglob("metadata.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict):
                continue
            if not hint_title:
                hint_title = str(meta.get("title") or "").strip()
            if not hint_author:
                authors = meta.get("authors")
                if isinstance(authors, list) and authors:
                    hint_author = str(authors[0] or "").strip()
                if not hint_author:
                    hint_author = str(meta.get("author") or "").strip()
            if not hint_asin:
                hint_asin = normalize_asin(meta.get("asin"))
            if not hint_series:
                hint_series = str(meta.get("series") or "").strip()
            if hint_asin or (hint_title and hint_author):
                break

    if not hint_title:
        hint_title = (title or "").strip()
    if not hint_author:
        hint_author = (author or "").strip()
    if not hint_title and not hint_author and not hint_asin:
        return

    seed_staging_metadata_hints(
        staging,
        title=hint_title,
        author=hint_author or None,
        asin=hint_asin or None,
        series=hint_series or None,
        force=True,
    )


def collect_staging_llm_context(staging: Path) -> dict[str, Any]:
    """File names/sizes + partial tags for OpenRouter metadata assist."""
    files: list[dict[str, Any]] = []
    if staging.is_dir():
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            # Skip bulky binaries in the prompt — name + size is enough.
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            rel = str(path.relative_to(staging))
            files.append({"path": rel, "size": size})
            if len(files) >= 40:
                break

    tags: dict[str, Any] = {}
    if staging.is_dir():
        for meta_path in staging.rglob("metadata.json"):
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(loaded, dict):
                tags = {
                    k: loaded.get(k)
                    for k in ("title", "author", "authors", "series", "asin", "narrator", "narrators")
                    if loaded.get(k)
                }
                break
    return {"files": files, "partial_tags": tags}

def _staging_library_roots() -> list[tuple[Path, tuple[str, ...]]]:
    """(library_root, staging_dirname_variants) for audiobook + ebook staging."""
    from app.services.ebook_pipeline import ebook_staging_dirname

    return [
        (
            Path(settings.audiobook_dir).resolve(),
            (audiobook_staging_dirname(), audiobook_staging_legacy_dirname()),
        ),
        (
            Path(settings.ebook_dir).resolve(),
            (ebook_staging_dirname(),),
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

    from app.services.ebook_pipeline import ebook_staging_dirname

    lib_specs = _staging_library_roots()
    staging_roots = all_staging_roots()
    candidates: list[Path] = [Path(raw)]
    ab_name = audiobook_staging_dirname()
    ab_legacy = audiobook_staging_legacy_dirname()
    eb_name = ebook_staging_dirname()
    ab_names = unorganized_dirnames()

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
                # Rewrite audiobook legacy staging name ↔ current staging name.
                for old, new in (
                    (ab_legacy, ab_name),
                    (ab_name, ab_legacy),
                    (LEGACY_UNORGANIZED_DIRNAME, UNORGANIZED_DIRNAME),
                    (UNORGANIZED_DIRNAME, LEGACY_UNORGANIZED_DIRNAME),
                ):
                    if old in mapped and old != new:
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
            if eb_name in mapped or any(n in mapped for n in ab_names):
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


def _cleanup_forge_temps(root: Path) -> int:
    """Remove m4b-tool / Chapter Forge temp dirs and obvious partial files.

    Safe to call after soft-fail: never deletes real source audio or ``.m4b``.
    """
    if not root.is_dir():
        return 0
    removed = 0
    for d in list(root.rglob("*-tmpfiles")):
        if not d.is_dir():
            continue
        try:
            shutil.rmtree(d)
            removed += 1
            logger.info("Removed leftover tmpfiles: %s", d)
        except OSError as e:
            logger.warning("Could not remove tmpfiles %s: %s", d, e)
    # Partial / temp files from m4b-tool and interrupted merges.
    patterns = ("*.tmp", "*.m4b.part", "*.m4b.tmp", "*.mp3.part", "*.m4a.part")
    for pattern in patterns:
        for path in list(root.rglob(pattern)):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
                logger.info("Removed forge temp file: %s", path)
            except OSError as e:
                logger.warning("Could not remove temp file %s: %s", path, e)
    return removed


def _prune_empty_dirs(root: Path, *, stop_at: Path | None = None) -> int:
    """Remove empty directories under ``root`` (deepest first). Keeps ``root`` itself."""
    if not root.is_dir():
        return 0
    root_res = root.resolve()
    stop = (stop_at or root).resolve()
    removed = 0
    try:
        dirs = sorted(
            (d for d in root.rglob("*") if d.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
    except OSError:
        return 0
    for d in dirs:
        try:
            d_res = d.resolve()
            if d_res == stop or d_res == root_res:
                continue
            if not d.is_dir():
                continue
            try:
                next(d.iterdir())
                continue
            except StopIteration:
                pass
            d.rmdir()
            removed += 1
            logger.info("Pruned empty directory: %s", d)
        except OSError:
            continue
    return removed


def _path_under_staging_roots(path: Path) -> bool:
    """True when ``path`` resolves under audiobook/ebook unorganized roots only."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in all_staging_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _staging_audio_hardlinked(staging: Path) -> bool:
    """True when every staging audio file shares an inode (nlink ≥ 2).

    Library Sweep stages via hardlink from the live library, so nlink≥2 means
    the library original remains after staging cleanup. Used to accept Folder
    Forge no-op moves without quarantining.
    """
    audio = _collect_audio(staging)
    if not audio:
        return False
    for path in audio:
        try:
            if path.stat().st_nlink < 2:
                return False
        except OSError:
            return False
    return True


def _cleanup_staging_after_folder_forge(
    staging: Path,
    *,
    force: bool = False,
) -> bool:
    """After successful Folder Forge (or finalize resume), wipe ``req_*`` staging.

    Removes temps, then deletes the whole staging tree when no audio remains —
    or when ``force=True`` (Folder Forge reported library moves; leftover samples
    / extras must not linger). Never deletes outside staging roots. Soft-fail /
    quarantine callers must not invoke this. Returns True if staging is gone.
    """
    if not staging.is_dir():
        return True
    if not _path_under_staging_roots(staging):
        logger.warning(
            "Refusing staging cleanup outside unorganized roots: %s", staging
        )
        return False
    _cleanup_forge_temps(staging)
    leftover = _collect_audio(staging)
    if leftover and not force:
        _prune_empty_dirs(staging)
        logger.warning(
            "Staging still has %d audio file(s) after Folder Forge: %s",
            len(leftover),
            staging,
        )
        return False
    if leftover and force:
        logger.info(
            "Force-removing staging with %d leftover audio after Folder Forge moves: %s",
            len(leftover),
            staging,
        )
    try:
        shutil.rmtree(staging)
        logger.info("Removed staging after Folder Forge: %s", staging)
        return True
    except OSError as e:
        logger.warning("Could not remove staging %s: %s", staging, e)
        _prune_empty_dirs(staging)
        try:
            if staging.is_dir() and not any(staging.iterdir()):
                staging.rmdir()
                return True
        except OSError:
            pass
        return not staging.exists()


def _remove_source_audio_after_m4b(scope: Path) -> int:
    """After a successful M4B merge, remove non-.m4b audio under ``scope``.

    Only runs when at least one ``.m4b`` exists in ``scope``. Call with the
    converted book folder for multi-book packs, or the whole staging tree after
    converting one of several format-variant folders (so leftover ``mp3/`` /
    ``AAC/`` parts are dropped). Keeps ``.m4b``, covers, and useful sidecars
    (``metadata.json`` / applied markers). Soft-fail callers must not invoke this.
    """
    audio = _collect_audio(scope)
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
    # Drop conversion temps and empty format / Tape / Part trees left behind.
    removed += _cleanup_forge_temps(scope)
    _prune_empty_dirs(scope)
    return removed


def _http_cover_from_mapping(data: Any, *, depth: int = 0) -> str | None:
    """Find first http(s) cover URL in a nested LibraForge / ABS metadata dict."""
    if depth > 6 or not isinstance(data, dict):
        return None
    for key in ("cover_url", "cover", "image_url"):
        val = str(data.get(key) or "").strip()
        if val.startswith("http"):
            return val
    # Prefer known LibraForge nests before a blind walk.
    for nest_key in ("marker", "sidecar", "book", "audible", "backup", "applied_tags"):
        nested = data.get(nest_key)
        if isinstance(nested, dict):
            found = _http_cover_from_mapping(nested, depth=depth + 1)
            if found:
                return found
    for value in data.values():
        if isinstance(value, dict):
            found = _http_cover_from_mapping(value, depth=depth + 1)
            if found:
                return found
    return None


def cover_url_from_staging(staging: Path) -> str | None:
    """Best-effort cover URL from metadata.json / libraforge.json for M4B --cover.

    Manual Review / Quick Review store the URL under nested paths such as
    ``marker.audible.cover_url`` and ``sidecar.book.cover_url`` (ABS
    ``metadata.json`` does not include ``cover_url``).
    """
    if not staging.is_dir():
        return None
    for meta_path in staging.rglob("metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found = _http_cover_from_mapping(meta)
        if found:
            return found
    for marker_path in staging.rglob("libraforge.json"):
        try:
            data = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found = _http_cover_from_mapping(data)
        if found:
            return found
    return None


def _str_field_from_mapping(data: Any, *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        val = data.get(key)
        if isinstance(val, list):
            parts = [str(x).strip() for x in val if str(x).strip()]
            if parts:
                return ", ".join(parts)
        text = str(val or "").strip()
        if text:
            return text
    return ""


def title_author_from_staging(staging: Path) -> tuple[str, str]:
    """Best-effort catalog title/author from applied staging sidecars."""
    if not staging.is_dir():
        return "", ""

    def _from_dict(data: dict[str, Any]) -> tuple[str, str]:
        title = _str_field_from_mapping(data, "title", "raw_title")
        author = _str_field_from_mapping(data, "author", "authorName", "authors")
        if not title or not author:
            for nest in ("book", "audible", "marker", "sidecar", "applied_tags", "backup"):
                nested = data.get(nest)
                if not isinstance(nested, dict):
                    continue
                # backup.applied_tags
                if nest == "backup":
                    applied = nested.get("applied_tags")
                    if isinstance(applied, dict):
                        nested = applied
                title = title or _str_field_from_mapping(nested, "title", "raw_title")
                author = author or _str_field_from_mapping(
                    nested, "author", "authorName", "authors"
                )
        return title, author

    for meta_path in sorted(
        list(staging.rglob("metadata.json")) + list(staging.rglob("*.metadata.json")),
        key=lambda p: (len(p.parts), str(p)),
    ):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict):
            title, author = _from_dict(meta)
            if title or author:
                return title, author

    for marker_path in sorted(
        staging.rglob("libraforge.json"),
        key=lambda p: (len(p.parts), str(p)),
    ):
        try:
            data = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            title, author = _from_dict(data)
            if title or author:
                return title, author
    return "", ""


async def refresh_request_display_metadata(
    request_id: int,
    staging: Path | None = None,
    *,
    title: str | None = None,
    author: str | None = None,
    cover_url: str | None = None,
) -> dict[str, str]:
    """Persist matched title/author/cover onto DownloadRequest for the Requests UI.

    Overwrites stale placeholder covers and swapped ABB title/author once
    Metadata Forge / Quick Review / identify succeeds.
    """
    from app.utils.websocket import ws_manager

    resolved_title = (title or "").strip()
    resolved_author = (author or "").strip()
    resolved_cover = (cover_url or "").strip()

    if staging is not None and staging.is_dir():
        if not resolved_cover:
            resolved_cover = cover_url_from_staging(staging) or ""
        if not resolved_title or not resolved_author:
            st_title, st_author = title_author_from_staging(staging)
            resolved_title = resolved_title or st_title
            resolved_author = resolved_author or st_author

    if resolved_author.lower() in {"", "unknown", "unknown author"}:
        resolved_author = ""

    if not resolved_title and not resolved_author and not resolved_cover:
        return {}

    updated: dict[str, str] = {}
    user_id: int | None = None
    status = ""
    detail: str | None = None
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if not req or req.status in ("cancelled", "admin_rejected"):
            return {}
        user_id = req.user_id
        status = req.status or ""
        detail = req.status_detail
        if resolved_title and resolved_title != (req.title or ""):
            req.title = resolved_title[:512]
            updated["title"] = req.title
        if resolved_author and resolved_author != (req.author or ""):
            req.author = resolved_author[:256]
            updated["author"] = req.author
        if resolved_cover.startswith("http") and resolved_cover != (req.cover_url or ""):
            req.cover_url = resolved_cover[:1024]
            updated["cover_url"] = req.cover_url
        if updated:
            await db.commit()

    if updated and user_id is not None:
        try:
            await ws_manager.send_to_user(
                user_id,
                {
                    "type": "status_update",
                    "request_id": request_id,
                    "status": status,
                    "detail": detail,
                    **updated,
                },
            )
        except Exception:
            logger.debug(
                "WS display refresh failed for request %s", request_id, exc_info=True
            )
        logger.info(
            "Refreshed request %s display metadata: %s",
            request_id,
            ", ".join(sorted(updated)),
        )
    return updated


async def _run_metadata_forge_once(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    phase_detail: str,
    provider: str | None = None,
) -> tuple[str, str]:
    """One Metadata Forge apply attempt.

    Returns ``("ok", "")``, ``("cancelled", "")``, or ``("fail", reason)``.
    Does not quarantine — caller decides assist vs quarantine.
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
            provider=provider,
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
            return "cancelled", ""
        return "fail", f"LibraForge Metadata Forge unavailable or failed: {e}"

    if libraforge.run_failed(report):
        err = report.get("error") or report.get("status") or "Metadata Forge failed"
        return "fail", str(err)[:500]

    if libraforge.metadata_auto_applied(report):
        if not staging_has_applied_metadata(staging):
            return "fail", (
                "Metadata Forge reported a write, but no applied libraforge.json / "
                "ASIN metadata.json was found in staging. Match may not have been "
                "persisted (permissions or apply race)."
            )
        return "ok", ""

    # Library Sweep / re-runs: LibraForge skips books that already carry apply
    # markers ("already processed"). That is success when on-disk evidence exists —
    # not a quarantine. Without this, sweep fails ~100% of already-fixed library books.
    if libraforge.metadata_already_processed(report):
        if staging_has_applied_metadata(staging):
            logger.info(
                "Metadata Forge skipped request %s (already processed) with applied "
                "markers — continuing pipeline",
                request_id,
            )
            return "ok", ""
        return "fail", (
            "Metadata Forge reported already processed, but no applied "
            "libraforge.json marker was found in staging."
        )

    return "fail", libraforge.quarantine_reason_from_report(report)


async def _run_metadata_forge_with_provider_fallback(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    phase_detail: str,
) -> tuple[str, str]:
    """Primary provider, then graphicaudio → soundbooththeater on miss.

    Current LibraForge (with abs-agg) also chains GA → SBT → hardcover / librivox /
    bigfinish / librofm / storygraph / deezer inside one Metadata Forge run.
    These Library Site outer retries only execute when the prior attempt did not
    apply, covering older LibraForge builds and forced-provider primary misses.
    """
    chain = libraforge.metadata_provider_chain()
    last_reason = "Metadata Forge failed"
    for idx, provider in enumerate(chain):
        detail = phase_detail if idx == 0 else f"Retrying metadata via {provider}…"
        if idx > 0:
            logger.info(
                "Metadata Forge provider fallback for request %s → %s",
                request_id,
                provider,
            )
        status, reason = await _run_metadata_forge_once(
            request_id,
            staging=staging,
            user_id=user_id,
            phase_detail=detail,
            provider=provider,
        )
        if status in ("ok", "cancelled"):
            return status, reason
        last_reason = reason or last_reason
    return "fail", last_reason


async def _llm_metadata_assist_retry(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    prior_reason: str,
) -> tuple[str, str]:
    """OpenRouter identify → seed hints → one Metadata Forge retry.

    Soft-fails to ``("fail", reason)`` on disable/low confidence/API errors.
    """
    from app.services import openrouter

    if not await openrouter.is_enabled():
        return "fail", prior_reason

    p = _pipeline()
    req_title = ""
    req_author = ""
    media_type = "audiobook"
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req_row = result.scalar_one_or_none()
        if req_row:
            req_title = req_row.title or ""
            req_author = req_row.author or ""
            media_type = req_row.media_type or "audiobook"
        await p._update_status(
            db, request_id, "metadata_forge", "Identifying with AI…"
        )

    context = {
        "request_title": req_title,
        "request_author": req_author,
        "media_type": media_type,
        "prior_failure": prior_reason[:500],
        **collect_staging_llm_context(staging),
    }

    try:
        identification = await openrouter.identify_book(context)
    except Exception as e:  # pragma: no cover - defensive soft-fail
        logger.warning("OpenRouter assist unexpected error for request %s: %s", request_id, e)
        return "fail", prior_reason

    if identification is None:
        logger.info(
            "OpenRouter assist unavailable/failed for request %s — quarantining",
            request_id,
        )
        return "fail", prior_reason

    threshold = await openrouter.get_confidence_threshold()
    if identification.confidence < threshold:
        logger.info(
            "OpenRouter assist low confidence for request %s (%.2f < %.2f) — quarantining",
            request_id,
            identification.confidence,
            threshold,
        )
        return "fail", (
            f"{prior_reason} | AI assist confidence {identification.confidence:.2f} "
            f"below {threshold:.2f}"
            + (f" ({identification.rationale})" if identification.rationale else "")
        )[:500]

    seed_staging_metadata_hints(
        staging,
        title=identification.title,
        author=identification.author or None,
        asin=identification.asin or None,
        series=identification.series or None,
        force=True,
    )
    logger.info(
        "LLM metadata assist for request %s: title=%r author=%r asin=%r "
        "confidence=%.2f — retrying Metadata Forge",
        request_id,
        identification.title,
        identification.author,
        identification.asin or "",
        identification.confidence,
    )

    return await _run_metadata_forge_with_provider_fallback(
        request_id,
        staging=staging,
        user_id=user_id,
        phase_detail="Retrying metadata with AI clues…",
    )


async def _apply_metadata_forge(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    phase_detail: str = "Matching metadata via LibraForge…",
    allow_llm_assist: bool = False,
) -> bool:
    """Run Metadata Forge apply (overwrite + replace_cover). Returns True if applied.

    On failure / no write evidence, optionally tries OpenRouter assist once
    (when ``allow_llm_assist`` and enabled), then quarantines and returns False.
    """
    status, reason = await _run_metadata_forge_with_provider_fallback(
        request_id,
        staging=staging,
        user_id=user_id,
        phase_detail=phase_detail,
    )
    if status == "ok":
        await refresh_request_display_metadata(request_id, staging)
        return True
    if status == "cancelled":
        return False

    if allow_llm_assist:
        status, reason = await _llm_metadata_assist_retry(
            request_id,
            staging=staging,
            user_id=user_id,
            prior_reason=reason,
        )
        if status == "ok":
            logger.info("Metadata Forge succeeded after LLM assist for request %s", request_id)
            await refresh_request_display_metadata(request_id, staging)
            return True
        if status == "cancelled":
            return False

    await _set_quarantine(request_id, reason or "Metadata Forge failed", staging)
    return False


def staging_has_applied_metadata(staging: Path) -> bool:
    """True when staging contains LibraForge *write* evidence (not mere seeds).

    Seeded ``metadata.json`` (catalog / LLM hints) often includes an ASIN before
    any tags are written. Treating that as applied made Quick Review skip real
    Apply and let the pipeline continue with narrator-as-author tags intact.
    Require an applied / manually_applied marker (or backup applied_tags).
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
    # Per-file companions: Book.mp3.libraforge.json
    for marker in staging.rglob("*.libraforge.json"):
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
    return False


def normalize_asin(value: Any) -> str:
    """Return a real Audible ASIN or '' (filters HAS_ASIN / empty sentinels)."""
    asin = str(value or "").strip().upper()
    if not asin or asin in _ASIN_SENTINELS:
        return ""
    if not _ASIN_RE.match(asin):
        return ""
    return asin


def _asin_from_sidecar_dict(data: dict[str, Any]) -> str:
    """Resolve ASIN from a libraforge.json dict.

    Mirrors LibraForge ownership for chaptering + scan inventory:
    curated ``book.asin``, then ``scan_cache.asin`` (embedded-tag cache from
    LF library scan), then fixer audible / applied tags, then top-level.
    """
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
        scan_cache.get("asin"),
        audible.get("asin"),
        applied.get("asin"),
        marker.get("asin"),
        data.get("asin"),
    ):
        asin = normalize_asin(candidate)
        if asin:
            return asin
    return ""


def _asin_from_filename(path: Path) -> str:
    match = _FILENAME_ASIN_RE.search(path.name)
    return normalize_asin(match.group(1)) if match else ""


def _asin_from_embedded_audio(audio_file: Path) -> str:
    """Best-effort embedded ASIN via mutagen (optional dependency)."""
    try:
        from mutagen.id3 import ID3  # type: ignore[import-untyped]
        from mutagen.mp4 import MP4, MP4FreeForm  # type: ignore[import-untyped]
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


def extract_asin_from_staging(staging: Path) -> str:
    """Best-effort ASIN using the same sources LibraForge inventory uses.

    Order: filename ``[B0…]`` token → libraforge.json (book / scan_cache /
    audible / applied) → ABS ``metadata.json`` → embedded mutagen tags.
    ``scan_cache.asin`` is critical: LF's "complete metadata" count is based
    on embedded tags cached there, not only curated ``book.asin``.
    """
    if not staging.is_dir():
        return ""
    audio_files = sorted(
        (p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda p: (len(p.parts), str(p)),
    )
    for audio in audio_files:
        asin = _asin_from_filename(audio)
        if asin:
            return asin
    # Prefer sidecars next to audio (shallow first via sorted path length).
    sidecars = sorted(
        staging.rglob("libraforge.json"),
        key=lambda p: (len(p.parts), str(p)),
    )
    sidecars.extend(
        sorted(
            staging.rglob("*.libraforge.json"),
            key=lambda p: (len(p.parts), str(p)),
        )
    )
    for path in sidecars:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            asin = _asin_from_sidecar_dict(data)
            if asin:
                return asin
    for meta_path in sorted(
        list(staging.rglob("metadata.json")) + list(staging.rglob("*.metadata.json")),
        key=lambda p: (len(p.parts), str(p)),
    ):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        asin = normalize_asin(meta.get("asin"))
        if asin:
            return asin
    for audio in audio_files:
        asin = _asin_from_embedded_audio(audio)
        if asin:
            return asin
    return ""


def primary_audio_for_chaptering(staging: Path) -> Path | None:
    """Primary ``.m4b`` for Chapter Forge, or None if none exists yet.

    Audible chapter markers must be embedded into an MP4-family file. Never
    point Chapter Forge at multipart ``.mp3`` folders (that path only writes
    sidecars / can leave m4b-tool's numeric 1–N markers in place).
    """
    audio = _collect_audio(staging)
    if not audio:
        return None
    m4bs = [f for f in audio if f.suffix.lower() == ".m4b"]
    if not m4bs:
        return None
    return max(m4bs, key=lambda p: p.stat().st_size if p.is_file() else 0)


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


async def _run_chapter_forge_step(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    should_abort,
    asin_override: str | None = None,
    force: bool = True,
) -> None:
    """Apply Audible chapters when ASIN + primary .m4b exist. Soft-fail only.

    When ``force`` is False, skip if the .m4b already has chapter markers (≥2).
    """
    p = _pipeline()
    asin = normalize_asin(asin_override) if asin_override else ""
    if not asin:
        asin = extract_asin_from_staging(staging)
    audio = primary_audio_for_chaptering(staging)

    if not asin:
        detail = "No ASIN in staging metadata — skipping Chapter Forge (keeping existing chapters)"
        logger.info("Chapter Forge skipped for request %s: no ASIN", request_id)
        async with async_session() as db:
            await p._update_status(db, request_id, "chapter_forge", detail)
            await p._report_progress(
                request_id, user_id, "chapter_forge", detail, progress_percent=100.0,
            )
        return

    if audio is None:
        # Multipart sources without an .m4b: wait for M4B convert; never rename parts.
        if needs_m4b_conversion(staging):
            detail = (
                f"ASIN {asin} present but no .m4b yet — skipping Chapter Forge "
                "(run after M4B convert)"
            )
        else:
            detail = f"ASIN {asin} present but no .m4b found — skipping Chapter Forge"
        logger.warning("Chapter Forge skipped for request %s: %s", request_id, detail)
        async with async_session() as db:
            await p._update_status(db, request_id, "chapter_forge", detail)
            await p._report_progress(
                request_id, user_id, "chapter_forge", detail, progress_percent=100.0,
            )
        return

    if not force:
        try:
            loaded = await libraforge.chaptering_load(staging_path_for_libraforge(audio))
            current = _extract_chapters_from_report(loaded if isinstance(loaded, dict) else {})
            if len(current) >= 2:
                detail = (
                    f"Chapter Forge skipped — {len(current)} existing markers "
                    f"(force_chapter_forge off)"
                )
                logger.info("Chapter Forge skipped for request %s: existing chapters", request_id)
                async with async_session() as db:
                    await p._update_status(db, request_id, "chapter_forge", detail)
                    await p._report_progress(
                        request_id, user_id, "chapter_forge", detail, progress_percent=100.0,
                    )
                return
        except Exception as e:
            logger.debug(
                "Chapter Forge existing-marker probe failed for %s: %s",
                request_id,
                e,
            )

    source_path = staging_path_for_libraforge(audio)
    async with async_session() as db:
        await p._update_status(
            db,
            request_id,
            "chapter_forge",
            f"Embedding Audible chapters into {audio.name} (ASIN {asin})…",
        )

    async def _on_chapters(state: dict[str, Any]) -> None:
        await _forge_progress(request_id, user_id, "chapter_forge", state)

    try:
        logger.info(
            "Chapter Forge request %s: source=%s asin=%s backend=audible-chapters",
            request_id,
            source_path,
            asin,
        )
        run_id = await libraforge.start_chaptering_run(
            source_path,
            asin=asin,
            backend="audible-chapters",
        )
        await _persist_staging(request_id, staging, run_id=run_id)
        report = await libraforge.wait_for_run(
            run_id,
            poll_seconds=2.0,
            timeout_seconds=settings.libraforge_chaptering_timeout,
            on_progress=_on_chapters,
            should_abort=should_abort,
        )
        if libraforge.run_failed(report):
            detail = (
                report.get("phase_detail")
                or report.get("error")
                or report.get("status")
                or "Chapter Forge failed"
            )
            # Soft-fail: keep existing chapters and continue to Folder Forge.
            # Never fall back to rename/restructure of source parts.
            logger.warning(
                "Chapter Forge failed for request %s (ASIN %s) — continuing: %s",
                request_id,
                asin,
                detail,
            )
            async with async_session() as db:
                await p._update_status(
                    db,
                    request_id,
                    "chapter_forge",
                    f"Chapter Forge failed ({detail}); keeping existing chapters"[:400],
                )
            return

        stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
        try:
            chapters_count = int(stats.get("chapters") or 0)
        except (TypeError, ValueError):
            chapters_count = 0
        embedded_into = str(stats.get("embedded_into") or "").strip()
        chapter_rows = chapter_embed.chapters_from_run_report(
            report if isinstance(report, dict) else {}
        )
        if not chapter_rows:
            chapter_rows = chapter_embed.chapters_from_libraforge_sidecar(audio)
        if chapters_count <= 0:
            chapters_count = len(chapter_rows)
        if chapters_count <= 0 and not chapter_rows:
            detail = (
                f"Audible returned no chapters for ASIN {asin}; "
                "keeping existing chapter markers"
            )
            logger.warning("Chapter Forge soft-fail for request %s: %s", request_id, detail)
            async with async_session() as db:
                await p._update_status(db, request_id, "chapter_forge", detail[:400])
            return

        # Upstream LibraForge only writes sidecars; Library embeds markers into the .m4b.
        # If a forked LibraForge already set embedded_into, skip a second remux.
        if not embedded_into:
            if not chapter_rows:
                detail = (
                    f"Chapter Forge saved Audible chapter data (ASIN {asin}, "
                    f"{chapters_count} chapters) but no chapter list was available to embed; "
                    "keeping existing chapters"
                )
                logger.warning("Chapter Forge soft-fail for request %s: %s", request_id, detail)
                async with async_session() as db:
                    await p._update_status(db, request_id, "chapter_forge", detail[:400])
                return
            duration = chapter_embed.duration_from_run_report(
                report if isinstance(report, dict) else {}
            )
            async with async_session() as db:
                await p._update_status(
                    db,
                    request_id,
                    "chapter_forge",
                    f"Embedding {len(chapter_rows)} Audible chapters into {audio.name}…",
                )
            try:
                await asyncio.to_thread(
                    chapter_embed.embed_chapters_into_audio,
                    audio,
                    chapter_rows,
                    duration=duration,
                    asin=asin,
                )
            except chapter_embed.ChapterEmbedError as e:
                detail = (
                    f"Chapter Forge looked up ASIN {asin} ({len(chapter_rows)} chapters) "
                    f"but embed failed ({e}); keeping existing chapters"
                )[:400]
                logger.warning("Chapter Forge embed soft-fail for request %s: %s", request_id, e)
                async with async_session() as db:
                    await p._update_status(db, request_id, "chapter_forge", detail)
                return
            chapters_count = len(chapter_rows)

        detail = (
            f"Embedded Audible chapters into {audio.name} "
            f"(ASIN {asin}, {chapters_count} chapters)"
        )
        logger.info("Chapter Forge success for request %s: %s", request_id, detail)
        async with async_session() as db:
            await p._update_status(db, request_id, "chapter_forge", detail)
            await p._report_progress(
                request_id, user_id, "chapter_forge", detail, progress_percent=100.0,
            )
    except libraforge.LibraForgeError as e:
        if "cancelled" in str(e).lower():
            return
        logger.warning(
            "Chapter Forge unavailable for request %s (ASIN %s) — continuing: %s",
            request_id,
            asin,
            e,
        )
        async with async_session() as db:
            await p._update_status(
                db,
                request_id,
                "chapter_forge",
                f"Chapter Forge skipped (error): {e}"[:400],
            )


_m4b_handoff_tasks: set[asyncio.Task] = set()


async def _run_m4b_handoff_continuation(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    title: str,
    author: str | None,
) -> None:
    """Background: finish M4B → chapters → folder after sweep handed off."""
    try:
        await run_forge_after_download(
            request_id,
            staging=staging,
            user_id=user_id,
            title=title,
            author=author,
            resume_from="m4b",
            handoff_m4b=False,
        )
    except Exception:
        logger.exception(
            "M4B handoff continuation failed for request %s", request_id
        )


async def run_forge_after_download(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    title: str,
    author: str | None = None,
    resume_from: str | None = None,
    stop_after: str | None = None,
    asin_override: str | None = None,
    handoff_m4b: bool = False,
) -> None:
    """Run Metadata Forge → M4B → re-apply → Chapter Forge → Folder Forge → ABS.

    After a successful M4B convert, Metadata Forge is re-applied (overwrite +
    replace_cover) because m4b-tool re-encode does not preserve embedded covers
    and ``enforce_m4b_output_metadata`` only writes text tags.

    Chapter Forge (Audible ``audible-chapters``) runs when an ASIN is present in
    staging sidecars. Missing ASIN skips cleanly (keeps existing chapters).
    Chapter Forge failures soft-continue — do not quarantine the whole book.

    ``resume_from``: None (full), ``m4b``, ``chapters``, ``folder``, or ``finalize``.
    Used after admin Manual Review in LibraForge.

    ``stop_after``: optional step id (``m4b`` / ``chapters`` / ``folder``) — return
    after that step completes so Quick Review can run steps interactively.

    ``handoff_m4b``: when True and conversion is needed, queue M4B (and the rest of
    the pipeline) as a background task and return immediately so Library Sweep can
    keep scanning. Encode concurrency stays 1 via ``m4b_queue``.
    """
    from app.services import library_ingest
    from app.services import instance_settings

    p = _pipeline()
    staging.mkdir(parents=True, exist_ok=True)
    lf_path = staging_path_for_libraforge(staging)
    await _persist_staging(request_id, staging)

    # Sweep / owned-upload: honor Library Sweep force/allow toggles.
    # Normal debrid downloads keep prior always-run behavior.
    is_local_ingest = False
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req_row = result.scalar_one_or_none()
        if req_row and library_ingest.is_local_ingest_request(req_row):
            is_local_ingest = True

    if is_local_ingest:
        skip_m4b = await instance_settings.get_effective_bool(
            "config.library_sweep_skip_m4b", default=False
        )
        allow_m4b = not skip_m4b
        force_metadata = await instance_settings.get_effective_bool(
            "config.library_sweep_force_metadata_forge", default=False
        )
        force_chapter = await instance_settings.get_effective_bool(
            "config.library_sweep_force_chapter_forge", default=False
        )
        force_folder = await instance_settings.get_effective_bool(
            "config.library_sweep_force_folder_forge", default=False
        )
    else:
        allow_m4b = True
        force_metadata = True
        force_chapter = True
        force_folder = True

    start_step = resume_from or "metadata"
    stop_at = (stop_after or "").strip().lower() or None
    if await p._is_cancelled(request_id):
        return

    async def _abort_if_cancelled() -> bool:
        return await p._is_cancelled(request_id)

    def _should_stop(step: str) -> bool:
        return stop_at == step

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

        # OpenRouter multi-book split (before single-book forge).
        try:
            from app.services import llm_assist

            multi = await llm_assist.maybe_handle_multi_book(
                request_id,
                staging=staging,
                user_id=user_id,
                title=title or "",
                author=author,
            )
            if multi in ("quarantined", "split"):
                return
        except Exception as e:
            logger.warning(
                "Multi-book assist soft-fail for request %s: %s", request_id, e
            )

        # Dual-format / sample prune suggestions (auto-delete only safe dupes).
        try:
            from app.services import llm_assist

            await llm_assist.maybe_auto_prune_or_suggest(request_id, staging=staging)
        except Exception as e:
            logger.warning("File prune assist soft-fail for request %s: %s", request_id, e)

        seed_staging_metadata_hints(staging, title=title, author=author)

        # Intelligent skip: applied markers already present and force is off.
        if not force_metadata and staging_has_applied_metadata(staging):
            logger.info(
                "Metadata Forge skipped for request %s (applied markers present; "
                "force_metadata_forge off)",
                request_id,
            )
            async with async_session() as db:
                await p._update_status(
                    db,
                    request_id,
                    "metadata_forge",
                    "Metadata already applied — skipping (force off)",
                )
            applied = True
        else:
            # Score ≥ min_score means match identity is trusted for auto-apply.
            # Apply itself is always full overwrite of all matched fields + cover
            # (never series_only / fill-if-empty), regardless of score quality.
            applied = await _apply_metadata_forge(
                request_id,
                staging=staging,
                user_id=user_id,
                phase_detail="Matching metadata via LibraForge…",
                allow_llm_assist=True,
            )
        if not applied:
            return

        # ASIN recovery when metadata applied but ASIN still missing.
        try:
            from app.services import llm_assist

            await llm_assist.maybe_recover_asin(
                request_id,
                staging=staging,
                title=title or "",
                author=author,
            )
        except Exception as e:
            logger.warning("ASIN assist soft-fail for request %s: %s", request_id, e)

        start_step = "m4b"

    if await p._is_cancelled(request_id):
        return

    # --- M4B (Pi LibraForge; global queue concurrency 1) ---
    if start_step == "m4b":
        m4b_produced_new_file = False
        needs_m4b = allow_m4b and needs_m4b_conversion(staging)
        if not allow_m4b and needs_m4b_conversion(staging):
            logger.info(
                "M4B skipped for request %s (library_sweep_skip_m4b on)",
                request_id,
            )
            async with async_session() as db:
                await p._update_status(
                    db,
                    request_id,
                    "m4b_convert",
                    "M4B disabled in Sweep settings — skipping conversion",
                )

        # Library Sweep: don't block the scanner on hours-long encodes.
        if handoff_m4b and needs_m4b:
            detail = "Queued for M4B — sweep continuing with next books…"
            async with async_session() as db:
                await p._update_status(db, request_id, "m4b_convert", detail)
            await p._report_progress(
                request_id, user_id, "m4b_convert", detail, progress_percent=None
            )
            task = asyncio.create_task(
                _run_m4b_handoff_continuation(
                    request_id,
                    staging=staging,
                    user_id=user_id,
                    title=title,
                    author=author,
                ),
                name=f"forge-m4b-handoff-{request_id}",
            )
            _m4b_handoff_tasks.add(task)
            task.add_done_callback(_m4b_handoff_tasks.discard)
            logger.info(
                "Request %s: M4B handed off to background (sweep non-blocking)",
                request_id,
            )
            return

        if needs_m4b:
            async def _on_m4b_queued(position: int, active_id: int | None) -> None:
                detail = m4b_queue.format_queue_detail(position, active_id)
                async with async_session() as db:
                    await p._update_status(db, request_id, "m4b_convert", detail)
                await p._report_progress(
                    request_id,
                    user_id,
                    "m4b_convert",
                    detail,
                    progress_percent=None,
                )

            async with m4b_queue.m4b_encode_slot(
                request_id,
                on_queued=_on_m4b_queued,
            ):
                if await p._is_cancelled(request_id):
                    return

                async with async_session() as db:
                    await p._update_status(
                        db, request_id, "m4b_convert", "Converting to M4B on Pi…"
                    )
                await p._report_progress(
                    request_id,
                    user_id,
                    "m4b_convert",
                    "Converting to M4B on Pi…",
                )

                async def _on_m4b(state: dict[str, Any]) -> None:
                    await _forge_progress(request_id, user_id, "m4b_convert", state)

                # LibraForge M4B only sees top-level audio in input_path (not recursive).
                source_dirs = m4b_source_dirs(staging)
                if not source_dirs:
                    source_dirs = [staging]
                # Dual-format torrents (mp3/ + AAC/): one convert; wipe all sources after.
                wipe_whole_staging = (
                    len(source_dirs) == 1
                    and source_dirs[0] != staging
                    and source_dirs[0].name.lower() in _FORMAT_DIR_NAMES
                )

                try:
                    for source_dir in source_dirs:
                        if await p._is_cancelled(request_id):
                            return
                        if not needs_m4b_conversion(source_dir) and source_dir != staging:
                            continue
                        input_path = staging_path_for_libraforge(source_dir)
                        loaded = await libraforge.m4b_load(input_path)
                        meta = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}
                        if not isinstance(meta, dict):
                            meta = {}
                        # m4b-tool re-encodes; embedded covers from Metadata Forge are NOT
                        # copied unless cover_url is passed for --cover. Always prefer the
                        # matched metadata cover from staging over stale torrent art.
                        cover = cover_url_from_staging(staging) or cover_url_from_staging(source_dir)
                        if cover:
                            meta = {**meta, "cover_url": cover}
                        book_title = (
                            str(meta.get("title") or "").strip()
                            or clean_catalog_title(title)
                            or title
                            or source_dir.name
                        )
                        if not meta.get("title"):
                            meta = {**meta, "title": book_title}
                        safe_title = downloader.sanitize_filename(book_title) or source_dir.name
                        # Keep Docker-style paths for LibraForge; output must sit next to
                        # the parts being merged (LibraForge does not recurse into subdirs).
                        default_out = f"{input_path.rstrip('/')}/{safe_title}.m4b"
                        output_path = str(loaded.get("output_path") or default_out)
                        out_parent = Path(output_path).parent.as_posix().rstrip("/")
                        if out_parent != input_path.rstrip("/"):
                            output_path = default_out
                        logger.info(
                            "M4B convert request %s: input=%s output=%s",
                            request_id,
                            input_path,
                            output_path,
                        )
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
                            detail = (
                                report.get("phase_detail")
                                or report.get("error")
                                or report.get("status")
                            )
                            # Soft-fail: keep going to Folder Forge with source audio
                            logger.warning(
                                "M4B conversion failed for request %s — continuing with source files: %s",
                                request_id,
                                detail,
                            )
                            async with async_session() as db:
                                await p._update_status(
                                    db,
                                    request_id,
                                    "m4b_convert",
                                    f"M4B conversion failed on Pi ({detail}); organizing source audio…"[:400],
                                )
                        else:
                            m4b_produced_new_file = True
                            # Never delete sources until convert succeeded.
                            if wipe_whole_staging:
                                _remove_source_audio_after_m4b(staging)
                            else:
                                _remove_source_audio_after_m4b(source_dir)
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
                # Soft-fail must keep source audio; temps from failed merges are still safe to drop.
                if not m4b_produced_new_file:
                    _cleanup_forge_temps(staging)
        else:
            skip_detail = (
                "M4B disabled in Sweep settings — skipping convert"
                if not allow_m4b
                else "Already a single M4B — skipping convert"
            )
            async with async_session() as db:
                await p._update_status(db, request_id, "m4b_convert", skip_detail)
                await p._report_progress(
                    request_id, user_id, "m4b_convert", skip_detail,
                    progress_percent=100.0,
                )
            _cleanup_forge_temps(staging)

        # M4B re-encode drops embedded covers / can leave stale tags on the new
        # file. Re-apply Metadata Forge (overwrite + replace_cover) onto the
        # post-convert .m4b before Folder Forge moves it into the library.
        if m4b_produced_new_file:
            if await p._is_cancelled(request_id):
                return
            # Re-seed catalog/LLM/QR author+ASIN next to the new .m4b so rematch
            # does not fall back to narrator-as-author embedded tags alone.
            try:
                _reseed_staging_hints_from_applied(staging, title=title, author=author)
            except Exception as e:
                logger.warning(
                    "Could not reseed metadata hints after M4B for request %s: %s",
                    request_id,
                    e,
                )
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

        if _should_stop("m4b"):
            async with async_session() as db:
                await p._update_status(
                    db,
                    request_id,
                    "quarantined",
                    "M4B step finished — continue Quick Review or pipeline when ready",
                )
                result = await db.execute(
                    select(DownloadRequest).where(DownloadRequest.id == request_id)
                )
                req_row = result.scalar_one_or_none()
                if req_row:
                    req_row.quarantine_reason = "Quick Review: M4B done — next Chapter Forge or Continue"
                    await db.commit()
            return

        start_step = "chapters"

    if await p._is_cancelled(request_id):
        return

    # --- Chapter Forge (Audible chapters → m4b markers) ---
    if start_step == "chapters":
        if not extract_asin_from_staging(staging):
            try:
                from app.services import llm_assist

                await llm_assist.maybe_recover_asin(
                    request_id,
                    staging=staging,
                    title=title or "",
                    author=author,
                )
            except Exception as e:
                logger.warning(
                    "ASIN assist (pre-chapter) soft-fail for request %s: %s",
                    request_id,
                    e,
                )
        await _run_chapter_forge_step(
            request_id,
            staging=staging,
            user_id=user_id,
            should_abort=_abort_if_cancelled,
            asin_override=asin_override,
            force=force_chapter,
        )
        # Chapter Forge / m4b-tool may leave *-tmpfiles; never deletes source audio.
        _cleanup_forge_temps(staging)
        if await p._is_cancelled(request_id):
            return
        if _should_stop("chapters"):
            async with async_session() as db:
                await p._update_status(
                    db,
                    request_id,
                    "quarantined",
                    "Chapter Forge step finished — continue Quick Review or pipeline when ready",
                )
                result = await db.execute(
                    select(DownloadRequest).where(DownloadRequest.id == request_id)
                )
                req_row = result.scalar_one_or_none()
                if req_row:
                    req_row.quarantine_reason = (
                        "Quick Review: chapters done — Continue for Folder Forge / finalize"
                    )
                    await db.commit()
            return
        start_step = "folder"

    if await p._is_cancelled(request_id):
        return

    # --- Folder Forge ---
    organizer_report: dict[str, Any] | None = None
    if start_step == "folder":
        if not force_folder and _staging_audio_hardlinked(staging):
            logger.info(
                "Folder Forge skipped for request %s (already hardlinked; force_folder_forge off)",
                request_id,
            )
            async with async_session() as db:
                await p._update_status(
                    db,
                    request_id,
                    "folder_forge",
                    "Already organized in library — skipping Folder Forge (force off)",
                )
            _cleanup_staging_after_folder_forge(staging, force=True)
            start_step = "finalize"
        else:
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
                organizer_report = report
            except libraforge.LibraForgeError as e:
                if "cancelled" in str(e).lower():
                    return
                raise RuntimeError(f"Folder Forge failed: {e}") from e

            # Folder Forge reporting success with zero moves while audio still sits
            # in staging means metadata was never applied (or tags are unusable).
            # Exception: Library Sweep hardlinks — files already live in the library
            # tree, so a no-op organize is expected; drop staging links and finalize.
            leftover_audio = _collect_audio(staging)
            moved = libraforge.organizer_moved_files(report)
            if leftover_audio and not moved:
                if _staging_audio_hardlinked(staging):
                    logger.info(
                        "Folder Forge made no moves for request %s but staging audio is "
                        "hardlinked into the library — treating as already organized",
                        request_id,
                    )
                    moved = True
                else:
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

            # Wipe req_* staging after successful organize (covers/nfo/empty format
            # dirs; force=True also drops leftover samples when moves succeeded).
            # Soft-fail / quarantine paths above return before this runs.
            _cleanup_staging_after_folder_forge(staging, force=moved)
            start_step = "finalize"

    elif start_step == "finalize":
        # Continue-from-finalize (Folder Forge already done manually / prior run):
        # drop empty/junk staging only — never force-wipe while audio remains.
        _cleanup_staging_after_folder_forge(staging, force=False)

    if await p._is_cancelled(request_id):
        return

    # --- Finalize (ABS) ---
    # Scan-only (no Quick Match / provider fetch). Then push LibraForge sidecars
    # into ABS so folder-name precedence cannot overwrite applied titles/covers.
    #
    # Library Sweep / owned upload: skip the full per-book ABS scan (very slow
    # at library scale). Sweep batches scans every N completions + on stop;
    # still sync moved-book metadata and invalidate the local cache.
    from app.services import library_ingest

    skip_full_abs_scan = False
    req_row = None
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req_row = result.scalar_one_or_none()
        if req_row and library_ingest.is_local_ingest_request(req_row):
            skip_full_abs_scan = True
        detail = (
            "Finalizing (batched ABS scan)…"
            if skip_full_abs_scan
            else "Scanning Audiobookshelf…"
        )
        await p._update_status(db, request_id, "finalizing", detail)

    try:
        if not skip_full_abs_scan:
            await audiobookshelf.scan_library_and_wait(timeout_seconds=240)
            await audiobookshelf.remove_items_with_issues()
        if organizer_report and libraforge.organizer_moved_files(organizer_report):
            sync_results = await audiobookshelf.sync_organizer_moves_to_abs(organizer_report)
            synced = sum(1 for r in sync_results if r.get("updated") or r.get("cover_updated"))
            if synced:
                logger.info(
                    "Pushed LibraForge metadata to ABS for %s/%s moved book(s) (req %s)",
                    synced,
                    len(sync_results),
                    request_id,
                )
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

    if skip_full_abs_scan and req_row is not None:
        try:
            from app.services import library_sweep as sweep_svc

            src = (getattr(req_row, "source", None) or "").strip().lower()
            if src == library_ingest.SOURCE_SWEEP:
                await sweep_svc.on_sweep_book_finalized()
        except Exception as e:
            logger.debug("Sweep batch ABS cadence hook failed: %s", e)

    # Ensure unorganized leftovers are gone on E2E success. When Folder Forge
    # reported moves, force-delete the req_* tree (samples/extras included).
    # Otherwise only wipe empty/junk trees — never soft-fail audio.
    organizer_did_move = bool(
        organizer_report and libraforge.organizer_moved_files(organizer_report)
    )
    if organizer_did_move:
        delete_request_staging_tree(request_id, staging_path_for_libraforge(staging))
    elif staging.is_dir():
        _cleanup_staging_after_folder_forge(staging, force=False)

    source_library_path: str | None = None
    async with async_session() as db:
        await p._update_status(db, request_id, "completed", "Ready in Audiobookshelf")
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            return
        source_library_path = (getattr(req, "source_library_path", None) or "").strip() or None
        # Clear staging_path after success (tree wiped when organize completed).
        req.staging_path = None
        req.quarantine_reason = None
        if organizer_did_move and source_library_path:
            # Source folder is superseded once Folder Forge lands files elsewhere.
            req.source_library_path = None
        await db.commit()
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

    # After commit: delete the pre-forge library folder when Folder Forge moved
    # the book to a new path (sweep hardlinks otherwise leave 2–4 copies).
    if organizer_did_move and source_library_path:
        try:
            _delete_superseded_source_library(
                source_library_path,
                organizer_report,
            )
        except Exception as e:
            logger.warning(
                "Could not delete superseded source library for request %s: %s",
                request_id,
                e,
            )


def _delete_superseded_source_library(
    source_library_path: str,
    organizer_report: dict[str, Any] | None,
) -> None:
    """Remove the original library folder after Folder Forge relocates the book.

    Safe guards: must sit under audiobook_dir, must not be staging, must not be
    equal to (or an ancestor of) any move target, and at least one target must
    still contain audio before deletion.
    """
    from app.services.library_media_delete import delete_tree_under_library

    raw = (source_library_path or "").strip()
    if not raw:
        return
    root = Path(settings.audiobook_dir).resolve()
    src = Path(raw).resolve()
    forbidden = unorganized_dirnames()
    try:
        rel = src.relative_to(root)
    except ValueError:
        logger.info("Skip source delete (outside library): %s", src)
        return
    if any(part in forbidden for part in rel.parts):
        logger.info("Skip source delete (staging path): %s", src)
        return
    if src == root:
        return

    targets = [Path(t).resolve() for t in libraforge.organizer_move_targets(organizer_report or {})]
    if not targets:
        logger.info("Skip source delete (no move targets): %s", src)
        return

    for target in targets:
        if src == target:
            logger.info("Skip source delete (organized in place): %s", src)
            return
        try:
            target.relative_to(src)
            # Target lives under source — deleting source would wipe the move.
            logger.info(
                "Skip source delete (target under source): src=%s target=%s",
                src,
                target,
            )
            return
        except ValueError:
            pass

    # Confirm at least one destination still has audio before removing source.
    has_keeper = False
    for target in targets:
        if not target.is_dir():
            continue
        if any(p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS for p in target.rglob("*")):
            has_keeper = True
            break
    if not has_keeper:
        logger.warning("Skip source delete (no audio at targets): %s", src)
        return

    delete_tree_under_library(src, root, forbidden)
    logger.info("Deleted superseded source library folder: %s", src)


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


def detect_pipeline_state(staging: Path) -> dict[str, Any]:
    """Filesystem-based progress for Quick Review (no DB flags required)."""
    has_metadata = staging_has_applied_metadata(staging)
    needs_m4b = needs_m4b_conversion(staging)
    audio = primary_audio_for_chaptering(staging)
    has_m4b = audio is not None
    asin = extract_asin_from_staging(staging)
    # Suggest next automated resume point.
    if needs_m4b:
        suggested = "m4b"
    elif has_m4b:
        suggested = "chapters"
    else:
        suggested = "folder"
    last_preview = read_chapter_preview(staging)
    return {
        "has_metadata": has_metadata,
        "needs_m4b": needs_m4b,
        "has_m4b": has_m4b,
        "m4b_path": staging_path_for_libraforge(audio) if audio else None,
        "asin": asin,
        "suggested_resume": suggested,
        "m4b_url": libraforge.public_m4b_url() or None,
        "chaptering_url": libraforge.public_chaptering_url() or None,
        "manual_review_url": libraforge.public_manual_review_url() or None,
        "chapter_preview": last_preview,
    }


def resolve_resume_from(
    staging: Path,
    *,
    resume_from: str | None = None,
    m4b_done: bool | None = None,
    chapters_done: bool | None = None,
) -> str:
    """Pick pipeline resume step from explicit value or wizard done-hints."""
    explicit = (resume_from or "").strip().lower()
    if explicit and explicit not in ("", "auto"):
        if explicit not in ("metadata", "m4b", "chapters", "folder", "finalize"):
            raise ValueError(f"Invalid resume_from '{resume_from}'")
        return explicit
    if chapters_done:
        return "folder"
    if m4b_done or not needs_m4b_conversion(staging):
        return "chapters"
    return "m4b"


CHAPTER_PREVIEW_FILENAME = "chapter_preview.json"


def _chapter_start_seconds(ch: dict[str, Any]) -> float:
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


def _normalize_chapter_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        return []
    out: list[dict[str, Any]] = []
    for i, ch in enumerate(raw):
        if isinstance(ch, dict):
            title = str(ch.get("title") or ch.get("name") or f"Chapter {i + 1}")
            out.append({"index": i, "title": title, "start": _chapter_start_seconds(ch)})
        elif isinstance(ch, str) and ch.strip():
            out.append({"index": i, "title": ch.strip(), "start": 0.0})
    return out


def _extract_chapters_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize chapter list from a LibraForge chaptering run / load payload.

    Audible-chapters runs nest the list under ``stats.chaptering_result.chapters``
    (``stats.chapters`` is only a count). ``/api/chaptering/load`` nests under
    ``result.chapters``.
    """
    if not isinstance(report, dict):
        return []
    stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
    nested_result = report.get("result") if isinstance(report.get("result"), dict) else {}
    chaptering_result = (
        stats.get("chaptering_result")
        if isinstance(stats.get("chaptering_result"), dict)
        else {}
    )
    raw_lists = [
        report.get("chapters"),
        report.get("audible_chapters"),
        report.get("preview_chapters"),
        nested_result.get("chapters"),
        chaptering_result.get("chapters"),
        stats.get("chapters_list"),
        # stats.chapters is usually an int count — skip non-lists via normalize.
        stats.get("chapters"),
    ]
    for raw in raw_lists:
        rows = _normalize_chapter_rows(raw)
        if rows:
            return rows
    titles = stats.get("chapter_titles") if isinstance(stats, dict) else None
    if isinstance(titles, list):
        return [
            {"index": i, "title": str(title or f"Chapter {i + 1}"), "start": 0.0}
            for i, title in enumerate(titles)
        ]
    return []


def chapter_preview_path(staging: Path) -> Path:
    return staging / CHAPTER_PREVIEW_FILENAME


def read_chapter_preview(staging: Path) -> dict[str, Any] | None:
    """Last Quick Review Chapter Forge compare payload (durable across refresh)."""
    path = chapter_preview_path(staging)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_chapter_preview(staging: Path, payload: dict[str, Any]) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["updated_at"] = datetime.now(timezone.utc).isoformat()
    chapter_preview_path(staging).write_text(
        json.dumps(body, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


async def preview_audible_chapters(
    request_id: int,
    *,
    asin: str,
) -> dict[str, Any]:
    """Fetch Audible chapters for visual confirm (no embed / no_save=True)."""
    p = _pipeline()
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        staging_str = (req.staging_path or "").strip()
        if not staging_str:
            raise ValueError("Request has no staging_path")
        user_id = req.user_id

    staging = resolve_staging_dir(staging_str)
    asin_n = normalize_asin(asin) or extract_asin_from_staging(staging)
    if not asin_n:
        raise ValueError("ASIN is required to preview Audible chapters")
    audio = primary_audio_for_chaptering(staging)
    if audio is None:
        raise ValueError("No .m4b found — run M4B convert before Chapter Forge")

    source_path = staging_path_for_libraforge(audio)
    # Existing markers for compare UI (best-effort via LibraForge load).
    current_chapters: list[dict[str, Any]] = []
    try:
        loaded = await libraforge.chaptering_load(source_path)
        current_chapters = _extract_chapters_from_report(loaded)
    except libraforge.LibraForgeError:
        logger.debug("chaptering_load failed for preview (non-fatal)", exc_info=True)

    async with async_session() as db:
        await p._update_status(
            db,
            request_id,
            "chapter_forge",
            f"Previewing Audible chapters (ASIN {asin_n})…",
        )

    run_id = await libraforge.start_chaptering_run(
        source_path,
        asin=asin_n,
        backend="audible-chapters",
        no_save=True,
    )
    await _persist_staging(request_id, staging, run_id=run_id)
    report = await libraforge.wait_for_run(
        run_id,
        poll_seconds=2.0,
        timeout_seconds=min(settings.libraforge_chaptering_timeout, 300.0),
    )
    if libraforge.run_failed(report):
        detail = (
            report.get("phase_detail")
            or report.get("error")
            or report.get("status")
            or "Chapter preview failed"
        )
        raise libraforge.LibraForgeError(str(detail))

    audible = _extract_chapters_from_report(report)
    stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
    chaptering_result = (
        stats.get("chaptering_result")
        if isinstance(stats.get("chaptering_result"), dict)
        else {}
    )
    resolved_asin = (
        normalize_asin(str(stats.get("asin") or chaptering_result.get("asin") or ""))
        or asin_n
    )
    chapters_n = 0
    try:
        chapters_n = int(stats.get("chapters") or len(audible) or 0)
    except (TypeError, ValueError):
        chapters_n = len(audible)
    chapters_n = chapters_n or len(audible)
    detail = (
        f"Chapter preview ready (ASIN {resolved_asin}, "
        f"{chapters_n} Audible / {len(current_chapters)} current)"
    )

    # Return to quarantine so Continue / wizard remain available (no re-notify —
    # admin is already in Quick Review).
    async with async_session() as db:
        await p._update_status(
            db,
            request_id,
            "quarantined",
            detail,
        )
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req_row = result.scalar_one_or_none()
        if req_row:
            req_row.quarantine_reason = "Quick Review: confirm Audible chapters then apply"
            await db.commit()

    payload = {
        "ok": True,
        "asin": resolved_asin,
        "source_path": source_path,
        "chapters": audible,
        "chapter_count": chapters_n,
        "current_chapters": current_chapters,
        "current_chapter_count": len(current_chapters),
        "backend": str(stats.get("backend") or chaptering_result.get("backend") or "audible-chapters"),
        "duration": stats.get("duration") or chaptering_result.get("duration"),
        "embedded_into": str(stats.get("embedded_into") or "").strip(),
        "status_detail": detail,
        "user_id": user_id,
    }
    try:
        write_chapter_preview(
            staging,
            {
                "asin": resolved_asin,
                "chapters": audible,
                "chapter_count": chapters_n,
                "current_chapters": current_chapters,
                "current_chapter_count": len(current_chapters),
                "backend": payload["backend"],
                "duration": payload.get("duration"),
                "status_detail": detail,
                "source_path": source_path,
            },
        )
    except OSError:
        logger.debug(
            "Could not persist chapter preview for request %s", request_id, exc_info=True
        )
    return payload


async def apply_audible_chapters(
    request_id: int,
    *,
    asin: str,
) -> dict[str, Any]:
    """Look up Audible chapters via LibraForge, then embed markers into staging .m4b."""
    p = _pipeline()
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        staging_str = (req.staging_path or "").strip()
        if not staging_str:
            raise ValueError("Request has no staging_path")
        user_id = req.user_id
        if req.quarantine_reason is not None:
            req.quarantine_reason = None
            await db.commit()

    staging = resolve_staging_dir(staging_str)
    asin_n = normalize_asin(asin) or extract_asin_from_staging(staging)
    if not asin_n:
        raise ValueError("ASIN is required to apply Chapter Forge")

    async def _abort() -> bool:
        return await p._is_cancelled(request_id)

    await _run_chapter_forge_step(
        request_id,
        staging=staging,
        user_id=user_id,
        should_abort=_abort,
        asin_override=asin_n,
    )
    _cleanup_forge_temps(staging)

    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req_row = result.scalar_one_or_none()
        detail = (req_row.status_detail if req_row else "") or ""
        # Keep wizard interactive — quarantine without re-push (admin present).
        await p._update_status(
            db,
            request_id,
            "quarantined",
            detail or "Chapter Forge finished — Continue for Folder Forge / finalize",
        )
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req_row = result.scalar_one_or_none()
        if req_row:
            req_row.quarantine_reason = "Quick Review: chapters applied — Continue remaining pipeline"
            await db.commit()
        embedded = "Embedded Audible chapters" in (detail or "")
        return {
            "ok": True,
            "asin": asin_n,
            "embedded": embedded,
            "status_detail": detail,
        }


def find_library_book_dir(title: str, author: str | None = None) -> Path | None:
    """Best-effort locate a finished book under the audiobook library root."""
    root = Path(settings.audiobook_dir)
    if not root.is_dir():
        return None
    title_slug = downloader.sanitize_filename(title or "").lower()
    author_slug = downloader.sanitize_filename(author or "").lower()
    if not title_slug:
        return None
    candidates: list[tuple[int, Path]] = []
    try:
        for path in root.rglob("*"):
            if not path.is_dir():
                continue
            parts = set(path.parts)
            if parts & unorganized_dirnames():
                continue
            if path.name in {"lost+found", ".git"} or path.name.startswith("."):
                continue
            name_l = path.name.lower()
            if title_slug not in name_l and name_l not in title_slug:
                continue
            if not _collect_audio(path):
                continue
            score = 0
            if name_l == title_slug:
                score += 3
            elif title_slug in name_l:
                score += 2
            if author_slug and author_slug in str(path).lower():
                score += 2
            candidates.append((score, path))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda t: (-t[0], len(str(t[1]))))
    return candidates[0][1]


async def prepare_pipeline_rerun(request_id: int) -> dict[str, Any]:
    """Re-stage a completed (or finished) audiobook for Quick Review / forge re-run.

    Behavior:
    - If staging still exists with audio → reuse it (reset to quarantined).
    - Else copy the library book folder into ``.unorganized/req_{id}_rerun_*``.
      The live library copy is left in place until Folder Forge moves/replaces.
    - Does **not** auto-start the pipeline; opens for Quick Review from metadata.
    - Uses ``_set_quarantine`` so admin-review push notifications still fire.
    """
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        if (req.media_type or "audiobook") == "ebook":
            raise ValueError("Ebook re-run is not supported here")
        if req.status not in (
            "completed",
            "failed",
            "admin_rejected",
            "quarantined",
            "cancelled",
        ):
            raise ValueError(
                f"Cannot re-run request in status '{req.status}' "
                "(wait for current forge step to finish)"
            )
        title = req.title
        author = req.author
        staging_str = (req.staging_path or "").strip()

    staging: Path | None = None
    source = "existing_staging"
    if staging_str:
        try:
            existing = resolve_staging_dir(staging_str)
            if existing.is_dir() and _collect_audio(existing):
                staging = existing
        except FileNotFoundError:
            staging = None

    if staging is None:
        lib_dir = find_library_book_dir(title, author)
        if lib_dir is None:
            raise FileNotFoundError(
                f"Could not find library folder for '{title}'"
                + (f" by {author}" if author else "")
                + " — re-stage manually or open LibraForge on the library path"
            )
        staging = ensure_unorganized_root() / (
            f"req_{request_id}_rerun_{downloader.sanitize_filename(title or 'book')[:60]}"
        )
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(lib_dir, staging, dirs_exist_ok=False)
        source = f"copied_from:{lib_dir}"

    await _persist_staging(request_id, staging)
    await _set_quarantine(
        request_id,
        (
            "Pipeline re-run — review metadata in Quick Review, then M4B / "
            "Chapter Forge / Continue. Library original left in place until "
            "Folder Forge moves the re-staged copy."
        ),
        staging,
    )
    state = detect_pipeline_state(staging)
    return {
        "ok": True,
        "id": request_id,
        "status": "quarantined",
        "staging_path": staging_path_for_libraforge(staging),
        "source": source,
        "pipeline": state,
        "message": "Re-staged for Quick Review — start from Metadata or Continue",
        "manual_review_url": state.get("manual_review_url"),
    }


async def continue_after_manual_review(
    request_id: int,
    *,
    resume_from: str | None = None,
    m4b_done: bool | None = None,
    chapters_done: bool | None = None,
    asin_override: str | None = None,
) -> None:
    """Resume forge pipeline after admin applied metadata in LibraForge Manual Review.

    The admin continue endpoint normally flips status to the resume step (and
    clears quarantine) before scheduling this task so UIs update immediately.
    ``m4b_convert`` / ``chapter_forge`` / ``folder_forge`` are accepted starts.
    """
    p = _pipeline()
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        allowed = (
            "quarantined",
            "metadata_forge",
            "m4b_convert",
            "chapter_forge",
            "folder_forge",
        )
        if req.status not in allowed:
            raise ValueError(f"Cannot continue request in status '{req.status}'")
        staging_str = (req.staging_path or "").strip()
        if not staging_str:
            raise ValueError("Request has no staging_path")
        try:
            staging = resolve_staging_dir(staging_str)
        except FileNotFoundError:
            staging = Path(staging_str)
            if not staging.is_dir():
                alt = Path(settings.audiobook_dir) / Path(staging_str).name
                if alt.is_dir():
                    staging = alt
                else:
                    raise FileNotFoundError(f"Staging folder missing: {staging_str}") from None
        user_id = req.user_id
        title = req.title
        author = req.author
        if req.quarantine_reason is not None:
            req.quarantine_reason = None
            await db.commit()

    step = resolve_resume_from(
        staging,
        resume_from=resume_from,
        m4b_done=m4b_done,
        chapters_done=chapters_done,
    )
    status_map = {
        "metadata": "metadata_forge",
        "m4b": "m4b_convert",
        "chapters": "chapter_forge",
        "folder": "folder_forge",
        "finalize": "finalizing",
    }
    async with async_session() as db:
        await p._update_status(
            db,
            request_id,
            status_map.get(step, "m4b_convert"),
            f"Resuming pipeline from {step}…",
        )

    await run_forge_after_download(
        request_id,
        staging=staging,
        user_id=user_id,
        title=title,
        author=author,
        resume_from=step,
        asin_override=asin_override,
    )
