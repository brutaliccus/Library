"""DIY ebook organizer: staging → identify → convert/embed → Author/Series/Title → Kavita.

No LibraForge / CWA. Kavita must exclude the ``unorganized`` folder from its library root.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import DownloadRequest, User
from app.services import downloader, kavita, push

logger = logging.getLogger(__name__)
settings = get_settings()

# Default — prefer settings.ebook_staging_dirname (Admin Config → Storage / Paths).
# Kavita must exclude this folder name from its library root (see docs/ebooks.md).
EBOOK_UNORGANIZED_DIRNAME = "unorganized"
# Calibre-Web trash + our staging — must never be ingested by Kavita.
EBOOK_KAVITA_EXCLUDED_DIRNAMES = frozenset({"caltrash", EBOOK_UNORGANIZED_DIRNAME})
# Root .kavitaignore patterns (Glob). Without these, caltrash poison Series tags
# (e.g. "???…") LocalizedSeries-merge into one mega-series on every scan.
EBOOK_KAVITA_IGNORE_PATTERNS = (
    "caltrash/*",
    "unorganized/*",
    "**/caltrash/**",
    "**/unorganized/**",
)


def ebook_staging_dirname() -> str:
    name = (getattr(settings, "ebook_staging_dirname", None) or EBOOK_UNORGANIZED_DIRNAME).strip()
    return name or EBOOK_UNORGANIZED_DIRNAME
EBOOK_EXTENSIONS = {
    ".epub",
    ".pdf",
    ".mobi",
    ".azw",
    ".azw3",
    ".fb2",
    ".djvu",
    ".cbz",
    ".cbr",
    ".txt",
}
# Prefer EPUB after convert; keep best remaining format if convert fails.
_FORMAT_RANK = {
    ".epub": 100,
    ".pdf": 80,
    ".azw3": 60,
    ".azw": 55,
    ".mobi": 50,
    ".fb2": 40,
    ".cbz": 30,
    ".cbr": 25,
    ".djvu": 20,
    ".txt": 10,
}

_ISBN13_RE = re.compile(r"(?:97[89][-\s]?)?(?:\d[-\s]?){9}[\dXx]")
_ISBN_DIGITS_RE = re.compile(r"^(?:\d{9}[\dXx]|\d{13})$")
# Filename / torrent junk that confuses Hardcover/OL search (trailing only).
# Allows "-copy" but not hyphen+digits so titles like "Catch-22" stay intact.
_EBOOK_DUP_SUFFIX_RE = re.compile(
    r"(?:(?:_|\s)(?:copy|\d+)|-copy| \(\d+\))$",
    re.IGNORECASE,
)
_EBOOK_FORMAT_TAG_RE = re.compile(
    r"\s*[\[(](?:epub|pdf|mobi|azw3?|kindle|retail|converted)[\])]|\.(?:epub|pdf|mobi|azw3?)$",
    re.IGNORECASE,
)
_EBOOK_NUMERIC_PARENS_RE = re.compile(r"\s*[\(\[]\s*\d+\s*[\)\]]")
_EBOOK_SYMBOL_RUN_RE = re.compile(r"[^\w\s'':&-]+", re.UNICODE)

EBOOK_PIPELINE_STATUSES = frozenset({
    "metadata_forge",
    "folder_forge",
    "finalizing",
})


def _pipeline():
    from app.services import pipeline as p
    return p


@dataclass
class EbookMeta:
    title: str
    author: str
    series: str | None = None
    series_index: str | None = None
    edition: str | None = None
    isbn13: str | None = None
    isbn10: str | None = None
    score: float = 0.0
    source: str = ""
    cover_url: str | None = None
    ambiguous: bool = False
    reason: str = ""


def ebook_unorganized_root() -> Path:
    return Path(settings.ebook_dir) / ebook_staging_dirname()


def ensure_ebook_kavita_ignores() -> Path:
    """Write library-root ``.kavitaignore`` so Kavita never scans staging/trash.

    Prior "path-coherent pin" fixes skipped pinning onto mega-series but left
    ``caltrash/`` + ``unorganized/`` inside the Kavita library root. Kavita's
    LocalizedSeries merge then matched every real series against a poison
    ``???…`` Series tag from ``caltrash/Unknown/1/1.epub`` and rebuilt an
    80+ file mega-series on every scan.
    """
    ebook_root = Path(settings.ebook_dir)
    try:
        ebook_root.mkdir(parents=True, exist_ok=True)
        ignore_path = ebook_root / ".kavitaignore"
        desired = "\n".join(EBOOK_KAVITA_IGNORE_PATTERNS) + "\n"
        existing = ""
        if ignore_path.is_file():
            try:
                existing = ignore_path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
        # Preserve admin-added patterns; ensure ours are present.
        lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
        changed = False
        for pat in EBOOK_KAVITA_IGNORE_PATTERNS:
            if pat not in lines:
                lines.append(pat)
                changed = True
        if changed or not ignore_path.is_file():
            ignore_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            logger.info("Wrote ebook .kavitaignore at %s", ignore_path)

        for dirname in EBOOK_KAVITA_EXCLUDED_DIRNAMES:
            folder = ebook_root / dirname
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            nested = folder / ".kavitaignore"
            if not nested.is_file():
                nested.write_text("*\n", encoding="utf-8")
    except OSError as e:
        logger.warning("Could not ensure ebook .kavitaignore under %s: %s", ebook_root, e)
    return ebook_root


def ensure_ebook_unorganized_root() -> Path:
    """Create ebook staging under the ebook library (excluded via .kavitaignore)."""
    ensure_ebook_kavita_ignores()
    root = ebook_unorganized_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Legacy marker (non-Kavita tools); real gate is .kavitaignore.
        ignore = root / ".ignore"
        if not ignore.exists():
            ignore.write_text("", encoding="utf-8")
        kavita_ignore = root / ".kavitaignore"
        if not kavita_ignore.is_file():
            kavita_ignore.write_text("*\n", encoding="utf-8")
    except OSError as e:
        logger.warning("Could not ensure ebook unorganized root %s: %s", root, e)
    return root


def ebook_staging_dir(request_id: int, title: str) -> Path:
    """Per-request landing folder under ebook staging (default ``/ebooks/unorganized``)."""
    slug = downloader.sanitize_filename(title or "book")[:80] or "book"
    return ensure_ebook_unorganized_root() / f"req_{request_id}_{slug}"


def staging_path_for_storage(staging: Path) -> str:
    """POSIX-style path as seen in Docker (e.g. ``/ebooks/unorganized/...``)."""
    try:
        resolved = staging.resolve()
    except OSError:
        resolved = staging
    root = Path(settings.ebook_dir).resolve()
    try:
        rel = resolved.relative_to(root)
        return str((Path(settings.ebook_dir) / rel).as_posix())
    except ValueError:
        return str(resolved.as_posix())


def ebook_destination_dir(request_id: int, author: str, book_title: str) -> Path:
    """Staging when pipeline is on; else legacy Author/Title under ebook_dir."""
    if settings.ebook_pipeline_enabled:
        return ebook_staging_dir(request_id, book_title or author)
    base = Path(settings.ebook_dir)
    return (
        base
        / downloader.sanitize_filename(author)
        / downloader.sanitize_filename(book_title)
    )


def _norm(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def clean_ebook_search_title(title: str) -> str:
    """Strip filename junk so catalog search keeps meaningful title words.

    Removes trailing ``(12)`` / ``_12`` / ``-copy`` markers, underscore separators,
    format tags, and pure-numeric parentheticals. Preserves word tokens (including
    titles that are themselves numeric like ``1984``).
    """
    t = (title or "").strip()
    if not t:
        return ""
    t = t.replace("_", " ")
    t = _EBOOK_FORMAT_TAG_RE.sub("", t)
    # Drop pure-numeric parentheticals anywhere (disk/volume markers in filenames).
    t = _EBOOK_NUMERIC_PARENS_RE.sub(" ", t)
    # Peel trailing duplicate/copy suffixes repeatedly.
    prev = None
    while prev != t:
        prev = t
        t = _EBOOK_DUP_SUFFIX_RE.sub("", t).strip()
    # Collapse leftover symbol runs to spaces; keep letters/digits/apostrophes.
    t = _EBOOK_SYMBOL_RUN_RE.sub(" ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–_|,.")
    # Drop trailing bare numeric tokens (e.g. "Book Title 12") but keep sole "1984".
    parts = t.split()
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    return " ".join(parts).strip()


def is_corrupt_metadata_value(value: str | None) -> bool:
    """True for mojibake / failed-decode placeholders (long ``????`` runs)."""
    from app.utils.book_series import is_corrupt_metadata_value as _corrupt

    return _corrupt(value)


def sanitize_ebook_meta(meta: EbookMeta) -> EbookMeta:
    """Drop corrupted identity fields so they are never written to OPF/sidecar/Kavita."""
    changed = False
    if is_corrupt_metadata_value(meta.title):
        meta.title = "Unknown"
        meta.score = min(float(meta.score or 0), 0.2)
        changed = True
    author_l = (meta.author or "").strip().lower()
    if is_corrupt_metadata_value(meta.author) or author_l in {".caltrash", "caltrash"}:
        meta.author = "Unknown"
        if author_l in {".caltrash", "caltrash"} or is_corrupt_metadata_value(author_l):
            meta.score = min(float(meta.score or 0), 0.2)
        changed = True
    if is_corrupt_metadata_value(meta.series):
        meta.series = None
        meta.series_index = None
        changed = True
    if is_corrupt_metadata_value(meta.edition):
        meta.edition = None
        changed = True
    if changed:
        note = "rejected corrupt metadata fields"
        reason = (meta.reason or "").strip()
        meta.reason = f"{reason} | {note}".strip(" |")[:500] if reason else note
    return meta


def _ebook_excluded_rel_part(part: str) -> bool:
    return part.casefold() in {n.casefold() for n in EBOOK_KAVITA_EXCLUDED_DIRNAMES}


def ebook_series_paths_coherent(dest_file: Path, paths: list[Path]) -> bool:
    """True when Kavita series files belong to this book / its series folder only.

    Rejects mixed-author mega-series, ``caltrash``/``unorganized`` paths, and
    multi-file series that do not share the same ``Author/Series`` (or shallow
    ``Author/Title``) prefix — so an author-root dump cannot pass as coherent.
    """
    if not paths:
        return False
    try:
        dest = Path(dest_file).resolve()
        ebook_root = Path(settings.ebook_dir).resolve()
        dest_rel = dest.relative_to(ebook_root)
    except (OSError, ValueError):
        return False
    if not dest_rel.parts or any(_ebook_excluded_rel_part(p) for p in dest_rel.parts):
        return False

    matched_dest = False
    authors: set[str] = set()
    prefixes: set[tuple[str, ...]] = set()
    for raw in paths:
        try:
            path = Path(raw).resolve()
            rel = path.relative_to(ebook_root)
        except (OSError, ValueError):
            return False
        if not rel.parts or any(_ebook_excluded_rel_part(p) for p in rel.parts):
            return False
        authors.add(rel.parts[0].casefold())
        if len(rel.parts) >= 2:
            prefixes.add(tuple(p.casefold() for p in rel.parts[:2]))
        else:
            prefixes.add((rel.parts[0].casefold(),))
        if path == dest:
            matched_dest = True

    if not matched_dest or len(authors) != 1:
        return False
    if len(paths) > 1 and len(prefixes) != 1:
        return False
    return True


async def repair_incoherent_kavita_ebook_series() -> dict[str, Any]:
    """Delete Kavita series that mix authors / trash paths / corrupt names, then rescan.

    Path-coherent pins alone cannot stop mega-series reform: Kavita rebuilds them
    from scanned files (especially poison caltrash Series tags). Call after ignores
    are written and before/after sweep batch scans.
    """
    ensure_ebook_kavita_ignores()
    out: dict[str, Any] = {
        "scanned": 0,
        "deleted": [],
        "skipped": 0,
        "errors": 0,
        "rescanned": False,
    }
    try:
        series_list = await kavita.get_all_series(
            formats=kavita.EBOOK_FORMATS, force_refresh=True
        )
    except Exception as e:
        logger.warning("Kavita mega-series repair: list failed: %s", e)
        out["errors"] += 1
        return out

    out["scanned"] = len(series_list)
    ebook_root = Path(settings.ebook_dir).resolve()

    for s in series_list:
        sid = s.get("id")
        if sid is None:
            continue
        try:
            sid_i = int(sid)
        except (TypeError, ValueError):
            continue

        name = str(s.get("name") or s.get("localizedName") or s.get("sortName") or "")
        reason: str | None = None
        if is_corrupt_metadata_value(name):
            reason = "corrupt_name"
        else:
            try:
                paths = await kavita.get_series_local_file_paths(sid_i)
            except Exception:
                out["skipped"] += 1
                continue
            if not paths:
                out["skipped"] += 1
                continue
            authors: set[str] = set()
            has_excluded = False
            for path in paths:
                try:
                    rel = Path(path).resolve().relative_to(ebook_root)
                except (OSError, ValueError):
                    has_excluded = True
                    break
                if not rel.parts or any(_ebook_excluded_rel_part(p) for p in rel.parts):
                    has_excluded = True
                    break
                authors.add(rel.parts[0].casefold())
            if has_excluded:
                reason = "excluded_path"
            elif len(authors) > 1:
                reason = f"mixed_authors:{len(authors)}"
            elif len(paths) >= 15 and len(authors) == 1:
                # Single-author dump of many unrelated titles under Author/ (no shared series folder).
                series_folders: set[str] = set()
                for path in paths:
                    try:
                        rel = Path(path).resolve().relative_to(ebook_root)
                    except (OSError, ValueError):
                        continue
                    if len(rel.parts) >= 2:
                        series_folders.add(rel.parts[1].casefold())
                if len(series_folders) >= 8:
                    reason = f"author_dump:{len(series_folders)}_folders"

        if not reason:
            out["skipped"] += 1
            continue

        try:
            ok = await kavita.delete_series(sid_i)
        except Exception as e:
            logger.warning("Kavita mega-series repair: delete %s failed: %s", sid_i, e)
            out["errors"] += 1
            continue
        if ok:
            out["deleted"].append({"id": sid_i, "name": name[:120], "reason": reason})
            logger.warning(
                "Deleted incoherent Kavita ebook series %s (%r) — %s",
                sid_i,
                name[:80],
                reason,
            )
        else:
            out["errors"] += 1

    if out["deleted"]:
        try:
            await kavita.scan_library_and_wait(timeout_seconds=240)
            kavita.invalidate_cache()
            out["rescanned"] = True
        except Exception as e:
            logger.warning("Kavita mega-series repair rescan failed: %s", e)
            out["errors"] += 1
            try:
                await kavita.scan_library()
                kavita.invalidate_cache()
            except Exception:
                pass
    return out


def title_similarity(a: str, b: str) -> float:
    """Token Jaccard similarity in [0, 1]."""
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    if union == 0:
        return 0.0
    return inter / union


def _collect_ebooks(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        f
        for f in folder.rglob("*")
        if f.is_file()
        and f.suffix.lower() in EBOOK_EXTENSIONS
        and "-tmpfiles" not in f.parts
    )


def pick_primary_ebook(folder: Path) -> Path | None:
    files = _collect_ebooks(folder)
    if not files:
        return None
    return max(files, key=lambda p: (_FORMAT_RANK.get(p.suffix.lower(), 0), -len(p.parts), p.name))


def extract_isbns_from_text(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for m in _ISBN13_RE.finditer(text):
            digits = "".join(c for c in m.group(0).upper() if c.isdigit() or c == "X")
            if len(digits) == 10 or (len(digits) == 13 and digits.startswith(("978", "979"))):
                if _ISBN_DIGITS_RE.match(digits) and digits not in seen:
                    seen.add(digits)
                    found.append(digits)
            # Strip leading 978/979-style hyphenated fragments that left 12 digits — skip.
    return found


def extract_isbns_from_staging(staging: Path) -> list[str]:
    texts: list[str] = [staging.name]
    for f in staging.rglob("*"):
        if f.is_file():
            texts.append(f.name)
            texts.append(f.stem)
    return extract_isbns_from_text(*texts)


def final_ebook_relative_dir(meta: EbookMeta) -> Path:
    """ABS/Folder Forge-shaped layout Kavita can ingest.

    ``{author}/{series}/{title}/`` or ``{author}/{series} [{edition}]/{title}/``;
    without series: ``{author}/{title}/``.
    """
    author = downloader.sanitize_filename(meta.author or "Unknown")
    title = downloader.sanitize_filename(meta.title or "Unknown")
    series = (meta.series or "").strip()
    edition = (meta.edition or "").strip()
    if series:
        series_folder = downloader.sanitize_filename(series)
        if edition:
            series_folder = downloader.sanitize_filename(f"{series} [{edition}]")
        return Path(author) / series_folder / title
    return Path(author) / title


def final_ebook_path(meta: EbookMeta, *, suffix: str = ".epub") -> Path:
    rel = final_ebook_relative_dir(meta)
    filename = downloader.sanitize_filename(meta.title or "book") + (
        suffix if suffix.startswith(".") else f".{suffix}"
    )
    return Path(settings.ebook_dir) / rel / filename


def ensure_series_index(meta: EbookMeta) -> EbookMeta:
    """Fill missing series / series_index from title cues so Kavita keeps distinct volumes."""
    from app.utils.book_series import (
        detect_series_from_title,
        extract_book_numbers_from_text,
        is_junk_series_hint,
        library_series_from_title,
    )

    if is_corrupt_metadata_value(meta.series) or is_junk_series_hint(meta.series):
        meta.series = None
        meta.series_index = None

    title = (meta.title or "").strip()
    detected = detect_series_from_title(title)
    inferred = library_series_from_title(title)
    if not meta.series:
        if inferred and inferred[0]:
            meta.series = inferred[0]
            if not meta.series_index and inferred[1]:
                meta.series_index = inferred[1]
        elif detected:
            meta.series = detected[0]
    if meta.series and not meta.series_index:
        if inferred and inferred[1]:
            meta.series_index = inferred[1]
        elif detected and detected[1]:
            meta.series_index = detected[1]
        else:
            nums = sorted(n for n in extract_book_numbers_from_text(title) if 0 < n < 200)
            if nums:
                n = nums[0]
                meta.series_index = str(int(n)) if float(n).is_integer() else str(n)
            else:
                # First book often has no number in the title.
                meta.series_index = "1"
    return meta


def _meta_from_catalog_book(book: dict, *, score: float, source: str) -> EbookMeta:
    authors = book.get("authors") or []
    if isinstance(authors, list):
        author = (authors[0] if authors else "") or book.get("author") or "Unknown"
    else:
        author = str(authors or book.get("author") or "Unknown")
    series = (book.get("seriesName") or book.get("series") or "").strip() or None
    seq = str(book.get("seriesBookNumber") or book.get("sequence") or "").strip() or None
    edition = (book.get("edition") or "").strip() or None
    return EbookMeta(
        title=(book.get("title") or "").strip() or "Unknown",
        author=str(author).strip() or "Unknown",
        series=series,
        series_index=seq,
        edition=edition if edition and len(edition) < 80 else None,
        isbn13=(book.get("isbn13") or "").strip() or None,
        isbn10=(book.get("isbn10") or "").strip() or None,
        score=score,
        source=source,
        cover_url=(book.get("coverUrl") or book.get("cover_url") or book.get("thumbnail") or None),
    )


async def _search_hardcover_meta(
    search_title: str, hint_author: str, *, min_score: float
) -> EbookMeta | None:
    """Title+author search via Hardcover. None when disabled, empty, or no usable hit."""
    from app.services import hardcover

    if not search_title or not await hardcover.get_api_key():
        return None
    try:
        hits = await hardcover.search_books(f"{search_title} {hint_author}".strip(), limit=8)
    except Exception as e:
        logger.warning("Hardcover ebook search failed: %s", e)
        return None

    ranked: list[tuple[float, dict]] = []
    for h in hits or []:
        ht = (h.get("title") or "").strip()
        if not ht:
            continue
        sim = title_similarity(search_title, ht)
        if sim < 0.35 and not hardcover._titles_compatible(search_title, ht):
            continue
        authors = h.get("authors") or []
        author_ok = True
        if hint_author and authors:
            author_ok = hardcover._authors_overlap(hint_author, authors)
        if hint_author and authors and not author_ok:
            continue
        score = 0.55 + 0.40 * sim
        if author_ok and hint_author:
            score += 0.08
        if hardcover._titles_compatible(search_title, ht):
            score = max(score, 0.72)
        if _norm(search_title) == _norm(ht) and hint_author and author_ok:
            score = max(score, 0.92)
        ranked.append((min(score, 0.99), h))

    if not ranked:
        return None

    ranked.sort(key=lambda x: x[0], reverse=True)
    best_score, best = ranked[0]
    # Ambiguous: second hit nearly as strong with different identity
    ambiguous = False
    if len(ranked) > 1:
        second_score, second = ranked[1]
        if second_score >= min_score and (best_score - second_score) < 0.08:
            if _norm(best.get("title") or "") != _norm(second.get("title") or ""):
                ambiguous = True
            elif (best.get("seriesName") or "") != (second.get("seriesName") or ""):
                ambiguous = True

    authors = best.get("authors") or []
    canon_author = (authors[0] if authors else "") or hint_author or "Unknown"
    series = (best.get("seriesName") or "").strip() or None
    seq = str(best.get("seriesBookNumber") or "").strip() or None
    return EbookMeta(
        title=(best.get("title") or search_title).strip(),
        author=str(canon_author).strip() or "Unknown",
        series=series,
        series_index=seq,
        isbn13=(best.get("isbn13") or "").strip() or None,
        isbn10=(best.get("isbn10") or "").strip() or None,
        score=best_score,
        source="hardcover",
        cover_url=(best.get("coverUrl") or best.get("cover_url") or None),
        ambiguous=ambiguous,
        reason=(
            "Ambiguous Hardcover matches"
            if ambiguous
            else f"Hardcover title/author match ({best_score:.2f})"
        ),
    )


async def _search_open_library_meta(
    search_title: str, hint_author: str, *, min_score: float
) -> EbookMeta | None:
    """Title+author search via Open Library. None when empty or no usable hit."""
    from app.services import google_books

    if not search_title:
        return None
    try:
        hits = await google_books.search_open_library(
            f"{search_title} {hint_author}".strip(), limit=8
        )
    except Exception as e:
        logger.warning("Open Library ebook search failed: %s", e)
        return None

    ranked: list[tuple[float, dict]] = []
    for h in hits or []:
        ht = (h.get("title") or "").strip()
        if not ht:
            continue
        sim = title_similarity(search_title, ht)
        if sim < 0.35:
            continue
        authors = h.get("authors") or []
        author_ok = True
        if hint_author and authors:
            author_ok = any(
                _norm(hint_author) in _norm(str(a)) or _norm(str(a)) in _norm(hint_author)
                for a in authors
            )
        score = 0.5 + 0.35 * sim
        if author_ok and hint_author:
            score += 0.10
        if _norm(search_title) == _norm(ht) and hint_author and author_ok:
            score = max(score, 0.88)
        ranked.append((min(score, 0.97), h))

    if not ranked:
        return None

    ranked.sort(key=lambda x: x[0], reverse=True)
    best_score, best = ranked[0]
    ambiguous = False
    if len(ranked) > 1:
        second_score, second = ranked[1]
        if second_score >= min_score and (best_score - second_score) < 0.08:
            if _norm(best.get("title") or "") != _norm(second.get("title") or ""):
                ambiguous = True

    meta = _meta_from_catalog_book(best, score=best_score, source="open_library")
    meta.ambiguous = ambiguous
    meta.reason = (
        "Ambiguous Open Library matches"
        if ambiguous
        else f"Open Library title/author match ({best_score:.2f})"
    )
    return meta


async def identify_ebook_metadata(
    *,
    staging: Path,
    title_hint: str,
    author_hint: str,
    google_volume_id: str | None = None,
    provider_order: list[str] | None = None,
) -> EbookMeta:
    """Resolve catalog metadata with confidence score.

    Order: request catalog volume → ISBN (OL / Google / ISBNdb) → title+author
    providers, tried in ``provider_order`` (default ``["hardcover"]``; Library
    Sweep passes ``["hardcover", "open_library"]``).
    """
    from app.services import google_books, isbndb, ol_catalog

    hint_title = (title_hint or "").strip()
    hint_author = (author_hint or "").strip()
    min_score = float(settings.ebook_min_score)

    # 1) Catalog volume attached to the request
    volume_id = (google_volume_id or "").strip()
    if volume_id:
        try:
            book = await google_books.get_catalog_volume(volume_id)
        except Exception as e:
            logger.warning("Catalog volume lookup failed for %s: %s", volume_id, e)
            book = None
        if book and (book.get("title") or "").strip():
            meta = _meta_from_catalog_book(book, score=1.0, source="catalog")
            if hint_title:
                sim = title_similarity(hint_title, meta.title)
                # Catalog id is authoritative when present; slight dampen if titles diverge wildly.
                if sim < 0.25 and hint_title.lower() not in ("unknown",):
                    meta.score = 0.85
                    meta.reason = f"Catalog volume title diverges from download ({sim:.2f})"
                else:
                    meta.reason = "Matched request catalog volume"
            if not meta.author or meta.author == "Unknown":
                meta.author = hint_author or meta.author
            return meta

    # 2) ISBN from staging filenames
    isbns = extract_isbns_from_staging(staging)
    for isbn in isbns:
        book = None
        source = ""
        try:
            if ol_catalog.catalog_ready():
                book = await ol_catalog.lookup_isbn(isbn)
                if book:
                    source = "ol_catalog"
        except Exception:
            book = None
        if not book:
            try:
                book = await isbndb.lookup_isbn(isbn)
                if book:
                    source = "isbndb"
            except Exception:
                book = None
        if not book:
            try:
                # Google Books ISBN query
                result = await google_books.search_volumes(f"isbn:{isbn}", max_results=1)
                books = (result or {}).get("books") or (result if isinstance(result, list) else [])
                if books:
                    book = books[0]
                    source = "google_books"
            except Exception:
                book = None
        if book and (book.get("title") or "").strip():
            meta = _meta_from_catalog_book(book, score=0.95, source=source or "isbn")
            meta.reason = f"ISBN {isbn} via {meta.source}"
            if not meta.author or meta.author == "Unknown":
                meta.author = hint_author or meta.author
            return meta

    # 3) Title + author → provider_order (default Hardcover only)
    search_title = clean_ebook_search_title(hint_title) or hint_title

    providers = provider_order or ["hardcover"]
    for provider in providers:
        if provider == "hardcover":
            meta = await _search_hardcover_meta(search_title, hint_author, min_score=min_score)
        elif provider == "open_library":
            meta = await _search_open_library_meta(search_title, hint_author, min_score=min_score)
        else:
            meta = None
        if meta is not None:
            return meta

    # Fallback: use request hints at low confidence → quarantine
    providers_label = "/".join(p.title().replace("_", " ") for p in providers)
    return EbookMeta(
        title=hint_title or "Unknown",
        author=hint_author or "Unknown",
        score=0.2,
        source="hint",
        reason=f"No catalog/ISBN/{providers_label} match",
    )


def _get_ebook_meta_bin() -> str | None:
    path = shutil.which("ebook-meta")
    if path:
        return path
    for candidate in ("/usr/bin/ebook-meta", "/usr/local/bin/ebook-meta"):
        if Path(candidate).exists():
            return candidate
    # Same install as ebook-convert (Calibre)
    convert = downloader._get_ebook_convert_path()
    if convert:
        sibling = Path(convert).with_name("ebook-meta")
        if sibling.exists():
            return str(sibling)
    return None


def download_ebook_cover_bytes(cover_url: str) -> tuple[bytes, str] | None:
    """Fetch cover image bytes and a file suffix (``.jpg`` / ``.png`` / ``.webp``)."""
    import urllib.request

    url = (cover_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    suffix = ".jpg"
    lower = url.lower()
    if ".png" in lower:
        suffix = ".png"
    elif ".webp" in lower:
        suffix = ".webp"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(10 * 1024 * 1024 + 1)
        if not data or len(data) > 10 * 1024 * 1024:
            return None
        if "png" in ctype:
            suffix = ".png"
        elif "webp" in ctype:
            suffix = ".webp"
        elif "jpeg" in ctype or "jpg" in ctype:
            suffix = ".jpg"
        return data, suffix
    except Exception as e:
        logger.warning("Could not download ebook cover %s: %s", url[:120], e)
        return None


def save_ebook_cover_beside(folder: Path, cover_url: str | None) -> Path | None:
    """Write ``cover.jpg`` (or png/webp) next to the ebook for shelf/Kavita cues."""
    url = (cover_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    fetched = download_ebook_cover_bytes(url)
    if not fetched:
        return None
    data, suffix = fetched
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # Prefer cover.jpg so existing cleanup keep-lists recognize it.
    dest = folder / ("cover.jpg" if suffix == ".jpg" else f"cover{suffix}")
    try:
        dest.write_bytes(data)
        # Remove alternate cover.* siblings so one canonical cover remains.
        for sibling in folder.glob("cover.*"):
            if sibling.resolve() == dest.resolve():
                continue
            if sibling.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                sibling.unlink(missing_ok=True)
        return dest
    except OSError as e:
        logger.warning("Could not write ebook cover beside %s: %s", folder, e)
        return None


def rename_ebook_to_metadata_title(ebook_path: Path, title: str) -> Path:
    """Rename an ebook file to a filesystem-safe metadata title (same folder).

    Collision-safe: reuses an identical-size existing target, otherwise picks
    ``Title (N).ext``. Does not move across directories (sibling volumes stay put).
    """
    src = Path(ebook_path)
    if not src.is_file():
        return src
    safe = downloader.sanitize_filename(title or src.stem) or src.stem
    suffix = src.suffix.lower() or ".epub"
    dest = src.with_name(f"{safe}{suffix}")
    try:
        if dest.resolve() == src.resolve():
            return src
    except OSError:
        if dest.name == src.name:
            return src

    if dest.exists():
        try:
            if dest.stat().st_size == src.stat().st_size and dest.stat().st_size > 0:
                if dest.resolve() != src.resolve():
                    src.unlink(missing_ok=True)
                return dest
        except OSError:
            pass
        stem = safe
        n = 2
        while True:
            candidate = src.with_name(f"{stem} ({n}){suffix}")
            if not candidate.exists():
                dest = candidate
                break
            n += 1

    try:
        src.rename(dest)
        logger.info("Renamed ebook %s → %s", src.name, dest.name)
        return dest
    except OSError as e:
        logger.warning("Could not rename ebook %s → %s: %s", src, dest.name, e)
        return src


async def embed_ebook_metadata(ebook_path: Path, meta: EbookMeta) -> bool:
    """Write title/author/series/cover (OPF) via Calibre ``ebook-meta``.

    Also persists a ``cover.*`` sidecar beside the ebook when a cover URL is
    available, even if Calibre embed is skipped (PDF / missing ebook-meta).
    """
    cover_saved = save_ebook_cover_beside(ebook_path.parent, meta.cover_url)

    if ebook_path.suffix.lower() not in {".epub", ".mobi", ".azw3", ".azw"}:
        return bool(cover_saved)
    ebook_meta = _get_ebook_meta_bin()
    if not ebook_meta:
        logger.warning("ebook-meta not found — skipping OPF embed for %s", ebook_path.name)
        return bool(cover_saved)

    cmd = [
        ebook_meta,
        str(ebook_path),
        "--title",
        meta.title or ebook_path.stem,
        "--authors",
        meta.author or "Unknown",
    ]
    if meta.series and not is_corrupt_metadata_value(meta.series):
        cmd.extend(["--series", meta.series])
        if meta.series_index:
            cmd.extend(["--index", str(meta.series_index)])
    else:
        # Standalone / cleared series: stamp Series=Title so Kavita never leaves
        # an empty/localized series key that LocalizedSeries-merges into trash.
        standalone = (meta.title or ebook_path.stem or "").strip()
        if standalone and not is_corrupt_metadata_value(standalone):
            cmd.extend(["--series", standalone, "--index", "1"])
    isbn = meta.isbn13 or meta.isbn10
    if isbn:
        cmd.extend(["--isbn", isbn])

    cover_tmp: Path | None = None
    cover_for_meta = cover_saved
    if cover_for_meta is None:
        cover_url = (meta.cover_url or "").strip()
        if cover_url.startswith("http"):
            fetched = download_ebook_cover_bytes(cover_url)
            if fetched:
                import tempfile

                data, suffix = fetched
                fd, tmp_name = tempfile.mkstemp(prefix="ebook-cover-", suffix=suffix)
                cover_tmp = Path(tmp_name)
                try:
                    with open(fd, "wb") as fh:
                        fh.write(data)
                    cover_for_meta = cover_tmp
                except OSError:
                    cover_tmp.unlink(missing_ok=True)
                    cover_tmp = None
    if cover_for_meta is not None:
        cmd.extend(["--cover", str(cover_for_meta)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
    finally:
        if cover_tmp and cover_tmp.exists():
            cover_tmp.unlink(missing_ok=True)

    if proc.returncode != 0:
        logger.warning(
            "ebook-meta failed for %s: %s",
            ebook_path.name,
            stderr.decode(errors="replace")[:400],
        )
        return bool(cover_saved)
    logger.info("Embedded ebook metadata into %s (series=%r)", ebook_path.name, meta.series)
    return True


async def pin_organized_ebook_to_kavita(
    dest_file: Path,
    meta: EbookMeta,
    *,
    summary: str | None = None,
    kavita_series_id: int | None = None,
    kavita_chapter_id: int | None = None,
    manually_applied: bool = False,
) -> dict[str, Any]:
    """After organize/apply: ensure cover file + Kavita identity/cover + sidecar ids.

    Mirrors ABS ``sync_book_dir_metadata_to_abs`` for ebooks.

    Never pins identity/cover onto an incoherent mega-series (mixed authors), and
    never falls back to fuzzy title matching for series ids — path ownership only.
    """
    from app.services.ebook_quick_review import (
        _read_applied_ebook_raw,
        write_applied_ebook_meta,
    )

    out: dict[str, Any] = {
        "series_id": None,
        "identity_updated": False,
        "cover_updated": False,
        "cover_file": None,
        "sidecar_updated": False,
        "pin_skipped_reason": None,
    }
    dest = Path(dest_file)
    if not dest.is_file():
        return out

    meta = sanitize_ebook_meta(ensure_series_index(meta))
    cover_path = save_ebook_cover_beside(dest.parent, meta.cover_url)
    if cover_path is not None:
        out["cover_file"] = str(cover_path)

    existing = _read_applied_ebook_raw(dest.parent) or {}
    if manually_applied or existing.get("manually_applied"):
        manually_applied = True
    chapter_id = kavita_chapter_id
    if chapter_id is None and existing.get("kavita_chapter_id") is not None:
        try:
            chapter_id = int(existing["kavita_chapter_id"])
        except (TypeError, ValueError):
            chapter_id = None

    # Prefer live path ownership. Stale sidecar series ids caused every book to
    # pin onto one mega-series after a bad Kavita merge.
    series_id: int | None = None
    try:
        series_id = await kavita.find_series_id_for_ebook_path(dest)
    except Exception as e:
        logger.debug("Kavita path lookup failed for %s: %s", dest, e)
        series_id = None

    if series_id is None and kavita_series_id is not None:
        try:
            candidate = int(kavita_series_id)
            paths = await kavita.get_series_local_file_paths(candidate)
            if any(Path(p).resolve() == dest.resolve() for p in paths):
                series_id = candidate
        except Exception:
            series_id = None

    if series_id is not None:
        try:
            paths = await kavita.get_series_local_file_paths(series_id)
        except Exception:
            paths = []
        series_name = ""
        try:
            all_s = await kavita.get_all_series(
                formats=kavita.EBOOK_FORMATS, force_refresh=False
            )
            detail = next(
                (x for x in all_s if int(x.get("id") or -1) == int(series_id)),
                None,
            )
            if isinstance(detail, dict):
                series_name = str(
                    detail.get("name")
                    or detail.get("localizedName")
                    or detail.get("sortName")
                    or ""
                )
        except Exception:
            series_name = ""
        incoherent = (
            not ebook_series_paths_coherent(dest, paths)
            or is_corrupt_metadata_value(series_name)
        )
        if incoherent:
            logger.warning(
                "Skipping Kavita identity/cover pin for %s — series %s is incoherent "
                "(%d files, name=%r); deleting series so scan can rebuild cleanly",
                dest,
                series_id,
                len(paths),
                series_name[:80],
            )
            out["pin_skipped_reason"] = "incoherent_series"
            try:
                await kavita.delete_series(series_id)
                out["incoherent_series_deleted"] = series_id
            except Exception as del_err:
                logger.warning(
                    "Could not delete incoherent Kavita series %s: %s", series_id, del_err
                )
            series_id = None
        else:
            out["series_id"] = series_id
            multi = len(paths) > 1
            series_display = (meta.series or "").strip() or meta.title
            kavita_name = series_display if multi else (meta.title or series_display)
            if not is_corrupt_metadata_value(kavita_name):
                try:
                    out["identity_updated"] = await kavita.update_series_identity(
                        series_id,
                        name=kavita_name or meta.title,
                        author=meta.author,
                        summary=summary,
                    )
                except Exception as e:
                    logger.warning("Kavita identity pin failed for series %s: %s", series_id, e)
            # Only stamp series cover for single-file series, or book 1 of a real series.
            seq = str(meta.series_index or "").strip()
            allow_cover = (not multi) or seq in {"", "1", "1.0"}
            if allow_cover and (meta.cover_url or "").strip().startswith("http"):
                try:
                    out["cover_updated"] = await kavita.set_series_cover_from_url(
                        series_id, meta.cover_url or ""
                    )
                except Exception as e:
                    logger.warning("Kavita cover pin failed for series %s: %s", series_id, e)

    try:
        write_applied_ebook_meta(
            dest.parent,
            meta,
            summary=summary,
            manually_applied=manually_applied,
            kavita_series_id=series_id,
            kavita_chapter_id=chapter_id,
        )
        out["sidecar_updated"] = True
    except Exception as e:
        logger.warning("Could not refresh ebook_applied.json for %s: %s", dest, e)
    return out


async def read_ebook_metadata(ebook_path: Path) -> dict[str, str | None]:
    """Read existing title/author/series/index from a file via calibre ``ebook-meta``.

    Used to preserve each file's own identity when applying series-level
    metadata (stamping one book's title/index into every file of a series
    collapsed distinct volumes into one in Kavita).
    """
    out: dict[str, str | None] = {
        "title": None,
        "author": None,
        "series": None,
        "series_index": None,
    }
    ebook_meta = _get_ebook_meta_bin()
    if not ebook_meta or ebook_path.suffix.lower() not in {".epub", ".mobi", ".azw3", ".azw"}:
        return out
    try:
        proc = await asyncio.create_subprocess_exec(
            ebook_meta,
            str(ebook_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return out
    except Exception as e:
        logger.debug("ebook-meta read failed for %s: %s", ebook_path.name, e)
        return out

    for line in stdout.decode(errors="replace").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "title":
            out["title"] = value
        elif key.startswith("author"):
            out["author"] = value.split("&")[0].split("[")[0].strip()
        elif key == "series":
            # calibre prints "Series              : Name #2.0"
            name, _, idx = value.rpartition("#")
            if idx and name:
                out["series"] = name.strip()
                out["series_index"] = idx.strip()
            else:
                out["series"] = value
    return out


def _cleanup_numbered_ebook_duplicates(dest_dir: Path, canonical: Path) -> None:
    """Remove ``Title (N).ext`` siblings when the canonical ``Title.ext`` exists."""
    stem = canonical.stem
    dup_re = re.compile(re.escape(stem) + r" \(\d+\)$")
    for sibling in dest_dir.iterdir():
        if not sibling.is_file() or sibling == canonical:
            continue
        if sibling.suffix.lower() != canonical.suffix.lower():
            continue
        if not dup_re.fullmatch(sibling.stem):
            continue
        try:
            sibling.unlink()
            logger.info("Removed duplicate ebook %s", sibling)
        except OSError as e:
            logger.warning("Could not remove duplicate ebook %s: %s", sibling, e)


def organize_ebook_files(staging: Path, meta: EbookMeta) -> Path:
    """Move primary ebook into final library layout; return destination file path.

    Same-sized existing destinations are reused (no ``Title (2).epub`` churn).
    Sibling duplicate ``(N)`` copies of the same stem are removed after a successful move.
    Writes ``ebook_applied.json`` beside the library file so shelf/detail prefer
    pipeline metadata over later Kavita scan drift.
    """
    meta = ensure_series_index(meta)
    primary = pick_primary_ebook(staging)
    if not primary:
        raise FileNotFoundError(f"No ebook files in staging: {staging}")

    dest_file = final_ebook_path(meta, suffix=primary.suffix.lower())
    dest_dir = dest_file.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    canonical = dest_file

    def _persist_override(target: Path) -> None:
        try:
            from app.services.ebook_quick_review import write_applied_ebook_meta

            # Prefer staging manual apply flag when present; else pipeline match.
            staging_applied = False
            try:
                from app.services.ebook_quick_review import load_applied_ebook_meta

                staging_applied = load_applied_ebook_meta(staging) is not None
            except Exception:
                staging_applied = False
            write_applied_ebook_meta(
                target.parent,
                meta,
                manually_applied=staging_applied,
            )
        except Exception as e:
            logger.warning("Could not write ebook_applied.json for %s: %s", target, e)

    # Identical re-download — keep the library file, drop the staging copy.
    if dest_file.exists():
        try:
            if dest_file.stat().st_size == primary.stat().st_size and dest_file.stat().st_size > 0:
                logger.info(
                    "Ebook already organized at %s (same size) — skipping move",
                    dest_file,
                )
                try:
                    primary.unlink(missing_ok=True)
                except OSError:
                    pass
                _cleanup_numbered_ebook_duplicates(dest_dir, canonical)
                _persist_override(dest_file)
                return dest_file
        except OSError:
            pass
        # Different content at the same path — use a numbered sibling.
        stem = dest_file.stem
        n = 2
        while True:
            candidate = dest_dir / f"{stem} ({n}){dest_file.suffix}"
            if not candidate.exists():
                dest_file = candidate
                break
            n += 1

    shutil.move(str(primary), str(dest_file))
    if dest_file == canonical:
        _cleanup_numbered_ebook_duplicates(dest_dir, canonical)
    _persist_override(dest_file)
    logger.info("Organized ebook → %s", dest_file)
    return dest_file


async def _persist_staging(request_id: int, staging: Path) -> None:
    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            return
        req.staging_path = staging_path_for_storage(staging)
        await db.commit()


async def _ebook_llm_identify_retry(
    *,
    staging: Path,
    title_hint: str,
    author_hint: str,
    google_volume_id: str | None,
    prior_reason: str,
    prior_meta: EbookMeta,
    provider_order: list[str] | None = None,
) -> EbookMeta | None:
    """OpenRouter identify then re-run catalog identify with seeded title/author.

    Soft-fails to None when assist is off or API errors.

    Soft-confidence (below the hard OpenRouter threshold but at/above ebook_min_score)
    still retries Hardcover/OL with the LLM title/author — matching what admins see
    when Search returns a 100% catalog hit. Raw LLM fields auto-apply only at/above
    the hard threshold.
    """
    from app.services import llm_assist, openrouter

    if not await openrouter.is_enabled():
        return None

    try:
        hit = await llm_assist.ebook_identify_assist(
            staging=staging,
            title_hint=title_hint,
            author_hint=author_hint,
            prior_reason=prior_reason,
        )
    except Exception as e:
        logger.warning("Ebook LLM assist unexpected error: %s", e)
        return None

    if not hit:
        return None

    threshold = await openrouter.get_confidence_threshold()
    min_score = float(settings.ebook_min_score)
    soft_floor = min(min_score, threshold)
    retry_title = (hit.title or title_hint or "").strip()
    retry_author = (hit.author or author_hint or "").strip()
    has_clues = bool(retry_title) and retry_title.lower() not in {"unknown", "untitled"}

    if hit.confidence < soft_floor or not has_clues:
        logger.info(
            "Ebook LLM assist low confidence (%.2f) — quarantining",
            hit.confidence,
        )
        prior_meta.reason = (
            f"{prior_reason} | AI assist confidence {hit.confidence:.2f} "
            f"below {threshold:.2f}"
            + (f" ({hit.rationale})" if hit.rationale else "")
        )[:500]
        return prior_meta

    if hit.confidence < threshold:
        logger.info(
            "Ebook LLM assist soft confidence (%.2f) — retrying catalog identify",
            hit.confidence,
        )
    else:
        logger.info(
            "Ebook LLM assist: title=%r author=%r confidence=%.2f — retrying identify",
            retry_title,
            retry_author,
            hit.confidence,
        )

    try:
        meta = await identify_ebook_metadata(
            staging=staging,
            title_hint=retry_title,
            author_hint=retry_author,
            google_volume_id=google_volume_id,
            provider_order=provider_order,
        )
    except Exception as e:
        logger.warning("Ebook identify retry after LLM failed: %s", e)
        if hit.confidence < threshold:
            prior_meta.reason = (
                f"{prior_reason} | AI assist confidence {hit.confidence:.2f}; "
                f"catalog retry failed: {e}"
            )[:500]
            return prior_meta
        return sanitize_ebook_meta(
            EbookMeta(
                title=retry_title or "Unknown",
                author=retry_author or "Unknown",
                series=hit.series or None,
                score=hit.confidence,
                source="openrouter",
                reason=hit.rationale or "OpenRouter ebook identify",
            )
        )

    meta = sanitize_ebook_meta(meta)
    if (
        meta.score >= min_score
        and not meta.ambiguous
        and not is_corrupt_metadata_value(meta.title)
        and meta.title != "Unknown"
    ):
        return meta

    if hit.confidence >= threshold:
        # Trust high-confidence LLM clues when catalog still weak.
        return sanitize_ebook_meta(
            EbookMeta(
                title=retry_title or meta.title,
                author=retry_author or meta.author,
                series=hit.series or meta.series,
                series_index=meta.series_index,
                edition=meta.edition,
                isbn13=meta.isbn13,
                isbn10=meta.isbn10,
                score=max(meta.score, hit.confidence),
                source="openrouter",
                cover_url=meta.cover_url,
                ambiguous=False,
                reason=hit.rationale or "OpenRouter ebook identify",
            )
        )

    prior_meta.reason = (
        f"{prior_reason} | AI assist confidence {hit.confidence:.2f} "
        f"below {threshold:.2f}; catalog still weak ({meta.score:.2f})"
        + (f" ({hit.rationale})" if hit.rationale else "")
    )[:500]
    return prior_meta


async def _set_quarantine(request_id: int, reason: str, staging: Path) -> None:
    p = _pipeline()
    if await p._is_cancelled(request_id):
        return
    async with async_session() as db:
        await p._update_status(db, request_id, "quarantined", reason[:500])
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if req:
            if req.status == "cancelled":
                return
            req.staging_path = staging_path_for_storage(staging)
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
                    "title": "Ebook quarantined — admin review",
                    "body": f"{req.title} (by {username}): {reason[:180]}",
                    "url": "/admin?tab=requests",
                })
            except Exception:
                logger.warning("Ebook quarantine admin push failed", exc_info=True)


def _delete_superseded_ebook_source(source_path: str, dest_file: Path) -> None:
    """Remove the original ebook library folder after Library Sweep re-organizes the book.

    Mirrors ``forge_pipeline._delete_superseded_source_library`` for ebooks.
    Safe guards: must sit under ``ebook_dir``, must not be (or contain) the
    unorganized staging folder, must not be the destination (or its parent),
    and the destination must already exist with an ebook file before delete.
    """
    from app.services.library_media_delete import delete_tree_under_library, get_ebook_forbidden_dirnames

    raw = (source_path or "").strip()
    if not raw:
        return
    root = Path(settings.ebook_dir).resolve()
    src = Path(raw).resolve()
    forbidden = get_ebook_forbidden_dirnames()
    try:
        rel = src.relative_to(root)
    except ValueError:
        logger.info("Skip ebook source delete (outside library): %s", src)
        return
    if any(part in forbidden for part in rel.parts):
        logger.info("Skip ebook source delete (staging path): %s", src)
        return
    if src == root:
        return

    dest = dest_file.resolve()
    if src == dest or src == dest.parent:
        logger.info("Skip ebook source delete (organized in place): %s", src)
        return
    try:
        dest.relative_to(src)
        logger.info(
            "Skip ebook source delete (destination under source): src=%s dest=%s",
            src,
            dest,
        )
        return
    except ValueError:
        pass

    if not dest.is_file() or dest.suffix.lower() not in EBOOK_EXTENSIONS:
        logger.warning("Skip ebook source delete (destination missing/not an ebook): %s", dest)
        return

    try:
        delete_tree_under_library(src, root, forbidden)
        logger.info("Deleted superseded ebook source folder %s", src)
    except Exception as e:
        logger.warning("Could not delete superseded ebook source %s: %s", src, e)


def wipe_staging(staging: Path) -> None:
    """Remove the request staging tree after a successful organize."""
    try:
        root = ebook_unorganized_root().resolve()
        path = staging.resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        logger.warning("Refusing to wipe path outside ebook unorganized: %s", staging)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        logger.info("Wiped ebook staging %s", path)


async def run_ebook_after_download(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    title: str,
    author: str | None,
    google_volume_id: str | None = None,
    resume_from: str = "metadata",
    convert_all_to_epub: bool = False,
    force_metadata: bool = False,
    provider_order: list[str] | None = None,
) -> None:
    """Post-download ebook pipeline: metadata → organize → Kavita finalize.

    ``resume_from``: ``metadata`` | ``folder`` | ``finalize``
    (no M4B step — ebooks never call LibraForge).

    ``convert_all_to_epub``: Library Sweep — convert PDF/MOBI/AZW/FB2/TXT (not
    CBZ/CBR) instead of just MOBI/AZW/AZW3.
    ``force_metadata``: when False and staging already carries a copied-in
    ``ebook_applied.json`` (Library Sweep re-processing an already organized
    book), reuse it instead of re-identifying. When True, always re-identify.
    ``provider_order``: title/author identify provider order (see
    ``identify_ebook_metadata``).
    """
    p = _pipeline()
    staging = Path(staging)
    await _persist_staging(request_id, staging)

    if await p._is_cancelled(request_id):
        return

    # Convert to EPUB in staging before identify/embed.
    try:
        await downloader.convert_ebooks_in_dir(staging, convert_all_to_epub=convert_all_to_epub)
    except Exception as e:
        logger.warning("Ebook convert in staging failed (continuing): %s", e)

    meta: EbookMeta | None = None
    dest_file: Path | None = None

    if resume_from in ("metadata",):
        async with async_session() as db:
            await p._update_status(db, request_id, "metadata_forge", "Identifying ebook metadata…")

        if not force_metadata:
            from app.services.ebook_quick_review import load_applied_ebook_meta

            meta = load_applied_ebook_meta(staging)

        if meta is None:
            meta = sanitize_ebook_meta(
                await identify_ebook_metadata(
                    staging=staging,
                    title_hint=title,
                    author_hint=author or "",
                    google_volume_id=google_volume_id,
                    provider_order=provider_order,
                )
            )
            min_score = float(settings.ebook_min_score)
            if (
                meta.ambiguous
                or meta.score < min_score
                or is_corrupt_metadata_value(meta.title)
                or meta.title == "Unknown"
            ):
                reason = meta.reason or f"Score {meta.score:.2f} below minimum {min_score:.2f}"
                if is_corrupt_metadata_value(meta.title) or meta.title == "Unknown":
                    reason = meta.reason or "Corrupt or unusable metadata title"
                if meta.ambiguous:
                    reason = meta.reason or "Ambiguous metadata matches"
                # OpenRouter identify → retry organize clues (same toggle/threshold).
                meta = await _ebook_llm_identify_retry(
                    staging=staging,
                    title_hint=title,
                    author_hint=author or "",
                    google_volume_id=google_volume_id,
                    prior_reason=reason,
                    prior_meta=meta,
                    provider_order=provider_order,
                )
                if meta is None or meta.ambiguous or meta.score < min_score:
                    fail_reason = reason
                    if meta is not None and meta.reason:
                        fail_reason = meta.reason
                    await _set_quarantine(request_id, fail_reason, staging)
                    return

        meta = ensure_series_index(meta)
        # Embed OPF tags on primary ebook while still in staging
        primary = pick_primary_ebook(staging)
        if primary:
            await embed_ebook_metadata(primary, meta)

    if await p._is_cancelled(request_id):
        return

    if resume_from in ("metadata", "folder"):
        if meta is None:
            # Prefer admin-selected Hardcover match from Quick Review when present.
            from app.services.ebook_quick_review import load_applied_ebook_meta

            applied = load_applied_ebook_meta(staging)
            if applied is not None:
                meta = applied
            else:
                # Continue-after-review: re-identify but skip score gate (admin approved).
                meta = await identify_ebook_metadata(
                    staging=staging,
                    title_hint=title,
                    author_hint=author or "",
                    google_volume_id=google_volume_id,
                    provider_order=provider_order,
                )
                if meta.score < 0.5:
                    # Prefer request hints when identification is still weak after review.
                    meta = EbookMeta(
                        title=(title or meta.title or "Unknown").strip(),
                        author=(author or meta.author or "Unknown").strip(),
                        series=meta.series,
                        series_index=meta.series_index,
                        edition=meta.edition,
                        isbn13=meta.isbn13,
                        isbn10=meta.isbn10,
                        score=max(meta.score, 0.7),
                        source=meta.source or "manual",
                        cover_url=meta.cover_url,
                        reason="Admin continue with request hints",
                    )
            primary = pick_primary_ebook(staging)
            if primary:
                await embed_ebook_metadata(primary, meta)

        async with async_session() as db:
            await p._update_status(db, request_id, "folder_forge", "Organizing ebook folders…")

        try:
            dest_file = organize_ebook_files(staging, meta)
        except Exception as e:
            await _set_quarantine(request_id, f"Organize failed: {e}", staging)
            return

        wipe_staging(staging)

        # Refresh Requests UI cover/title/author from identified metadata
        # (overwrite stale placeholders / swapped ABB fields).
        if meta.cover_url or meta.title or meta.author:
            from app.services.forge_pipeline import refresh_request_display_metadata

            try:
                await refresh_request_display_metadata(
                    request_id,
                    title=meta.title or None,
                    author=meta.author or None,
                    cover_url=meta.cover_url or None,
                )
            except Exception as e:
                logger.warning(
                    "Could not refresh request display after ebook identify for %s: %s",
                    request_id,
                    e,
                )

        logger.info("Ebook organized for request %s → %s", request_id, dest_file)

        # Delete the pre-sweep source folder once the book lands at a new path
        # (Library Sweep hardlinks otherwise leave duplicate copies on disk).
        source_library_path = ""
        async with async_session() as db:
            result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
            req = result.scalar_one_or_none()
            source_library_path = (getattr(req, "source_library_path", None) or "").strip() if req else ""
            if req and source_library_path:
                req.source_library_path = None
                await db.commit()
        if source_library_path:
            try:
                _delete_superseded_ebook_source(source_library_path, dest_file)
            except Exception as e:
                logger.warning(
                    "Could not delete superseded ebook source for request %s: %s",
                    request_id,
                    e,
                )

    if await p._is_cancelled(request_id):
        return

    # Finalize — Kavita scan, then pin identity/cover like ABS sidecar sync.
    async with async_session() as db:
        await p._update_status(db, request_id, "finalizing", "Scanning Kavita…")

    try:
        # Modest wait so pin can resolve series_id; sweep also batch-scans + resyncs.
        await kavita.scan_library_and_wait(timeout_seconds=45, poll_interval=3.0)
        kavita.invalidate_cache()
    except Exception as e:
        logger.warning("Kavita scan after ebook organize failed (non-fatal): %s", e)
        try:
            await kavita.scan_library()
            kavita.invalidate_cache()
        except Exception:
            pass
        try:
            async with async_session() as db:
                await push.notify_admins(db, {
                    "type": "error",
                    "title": "Library Scan Failed",
                    "body": f"Kavita scan failed after organizing {title}: {e}",
                    "url": "/admin?tab=requests",
                })
        except Exception:
            pass

    if meta is not None and dest_file is not None:
        try:
            # Always persist cover sidecar + ebook_applied.json; Kavita pin is best-effort.
            pin = await pin_organized_ebook_to_kavita(dest_file, meta)
            logger.info(
                "Pinned ebook to Kavita for request %s: series=%s identity=%s cover=%s",
                request_id,
                pin.get("series_id"),
                pin.get("identity_updated"),
                pin.get("cover_updated"),
            )
        except Exception as e:
            logger.warning("Ebook Kavita pin failed for request %s: %s", request_id, e)

    async with async_session() as db:
        await p._update_status(db, request_id, "completed", "Ready in Kavita")
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            return
        # Clear staging_path after success (tree wiped)
        req.staging_path = None
        req.quarantine_reason = None
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
            await push.notify_download_complete(req.user_id, title, "Kavita", db)
        except Exception as e:
            logger.warning("Push notification failed (non-fatal): %s", e)


async def continue_ebook_after_review(request_id: int) -> None:
    """Resume ebook pipeline after admin review — skip confidence gate, organize → finalize."""
    p = _pipeline()
    from app.services.forge_pipeline import resolve_staging_dir

    async with async_session() as db:
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        if req.status not in ("quarantined", "metadata_forge", "folder_forge"):
            raise ValueError(f"Cannot continue ebook request in status '{req.status}'")
        staging_str = (req.staging_path or "").strip()
        if not staging_str:
            raise ValueError("Request has no staging_path")
        user_id = req.user_id
        title = req.title
        author = req.author
        volume_id = getattr(req, "google_volume_id", None)
        if req.quarantine_reason is not None:
            req.quarantine_reason = None
            await db.commit()
        if req.status in ("quarantined", "metadata_forge"):
            await p._update_status(
                db,
                request_id,
                "folder_forge",
                "Resuming ebook organize after review…",
            )

    staging = resolve_staging_dir(staging_str)
    await run_ebook_after_download(
        request_id,
        staging=staging,
        user_id=user_id,
        title=title,
        author=author,
        google_volume_id=volume_id,
        resume_from="folder",
    )
