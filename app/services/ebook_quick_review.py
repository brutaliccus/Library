"""In-app ebook metadata matcher: Hardcover + Google Books + Open Library -> select -> apply.

Mirrors audiobook Quick Review's load/search/apply UX without LibraForge.
Applied matches are persisted as ``ebook_applied.json`` so Continue organize
uses the admin-selected identity instead of re-running auto-identify.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.models import DownloadRequest
from app.services.ebook_pipeline import (
    EbookMeta,
    _collect_ebooks,
    clean_ebook_search_title,
    embed_ebook_metadata,
    pick_primary_ebook,
    rename_ebook_to_metadata_title,
    title_similarity,
)
from app.services.forge_pipeline import (
    clean_catalog_title,
    resolve_staging_dir,
    staging_path_for_libraforge,
)
from app.services.quick_review import _folder_title_hint

logger = logging.getLogger(__name__)

APPLIED_META_FILENAME = "ebook_applied.json"

_SOURCE_LABELS = {
    "hardcover": "Hardcover",
    "google_books": "Google Books",
    "open_library": "Open Library",
    "ol_catalog": "Open Library",
}


class EbookQuickReviewError(ValueError):
    """User-facing ebook quick review failure."""


def list_ebook_staging_targets(staging: Path) -> list[dict[str, Any]]:
    """Ebook files/folders under staging suitable for metadata review."""
    staging_res = staging.resolve()
    files = _collect_ebooks(staging_res)
    if not files:
        return []

    parents: list[Path] = []
    seen: set[Path] = set()
    for f in files:
        parent = f.parent.resolve()
        if parent in seen:
            continue
        seen.add(parent)
        parents.append(parent)

    if len(parents) == 1 and parents[0] == staging_res:
        parents = [staging_res]

    targets: list[dict[str, Any]] = []
    for parent in parents:
        try:
            rel = parent.relative_to(staging_res)
        except ValueError:
            continue
        rel_posix = rel.as_posix() if str(rel) != "." else ""
        ebooks = _collect_ebooks(parent)
        targets.append(
            {
                "relative_path": rel_posix,
                "path": staging_path_for_libraforge(parent),
                "display_name": parent.name if rel_posix else staging_res.name,
                "file_count": len(ebooks),
                "is_grouped": len(ebooks) > 1,
            }
        )
    return targets


def write_applied_ebook_meta(
    staging: Path,
    meta: EbookMeta,
    *,
    summary: str | None = None,
    manually_applied: bool = True,
    kavita_series_id: int | None = None,
    kavita_chapter_id: int | None = None,
) -> Path:
    """Persist pipeline/admin ebook identity beside the ebook file (ABS sidecar analogue).

    Written next to library ebooks as ``ebook_applied.json`` so Kavita scans cannot
    wipe manual titles from the Library Site shelf (file-scoped; one book folder).
    """
    from app.services.ebook_pipeline import sanitize_ebook_meta
    from app.utils.book_series import is_corrupt_metadata_value

    meta = sanitize_ebook_meta(meta)
    staging = staging.resolve()
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / APPLIED_META_FILENAME
    summary_text = (summary or "").strip()
    if is_corrupt_metadata_value(summary_text):
        summary_text = ""
    series = None if is_corrupt_metadata_value(meta.series) else meta.series
    payload = {
        "applied": True,
        "manually_applied": bool(manually_applied),
        "source": meta.source or "manual",
        "kavita_series_id": kavita_series_id,
        "kavita_chapter_id": kavita_chapter_id,
        "meta": {
            "title": meta.title,
            "author": meta.author,
            "series": series,
            "series_index": meta.series_index if series else None,
            "edition": meta.edition,
            "isbn13": meta.isbn13,
            "isbn10": meta.isbn10,
            "score": meta.score,
            "source": meta.source,
            "cover_url": meta.cover_url,
            "reason": meta.reason,
            "summary": summary_text or None,
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _read_applied_ebook_raw(folder: Path) -> dict[str, Any] | None:
    """Load raw ``ebook_applied.json`` from a folder, or None."""
    if not folder or not folder.is_dir():
        return None
    path = folder / APPLIED_META_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not (data.get("applied") or data.get("manually_applied")):
        return None
    return data


def load_applied_ebook_override(folder_or_file: Path | str | None) -> dict[str, Any] | None:
    """Return override fields from ``ebook_applied.json`` beside a library ebook.

    Accepts a file path (uses parent) or a book directory. Prefer these fields over
    live Kavita metadata when building shelf/detail responses (mirrors ABS sidecar
    precedence after scan).
    """
    if not folder_or_file:
        return None
    path = Path(folder_or_file)
    folder = path.parent if path.suffix else path
    data = _read_applied_ebook_raw(folder)
    if not data:
        return None
    raw = data.get("meta") if isinstance(data.get("meta"), dict) else data
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    summary = str(raw.get("summary") or raw.get("description") or "").strip()
    return {
        "title": title,
        "author": str(raw.get("author") or "").strip(),
        "series": str(raw.get("series") or "").strip(),
        "series_index": str(raw.get("series_index") or raw.get("sequence") or "").strip(),
        "edition": str(raw.get("edition") or "").strip(),
        "isbn13": str(raw.get("isbn13") or "").strip(),
        "isbn10": str(raw.get("isbn10") or "").strip(),
        "cover_url": str(raw.get("cover_url") or "").strip(),
        "summary": summary,
        "source": str(raw.get("source") or data.get("source") or "manual").strip() or "manual",
        "manually_applied": bool(data.get("manually_applied")),
    }


def apply_ebook_override_fields(
    item: dict[str, Any],
    override: dict[str, Any] | None,
    *,
    multi_volume: bool = False,
) -> dict[str, Any]:
    """Prefer saved override fields over live Kavita values (in-place)."""
    if not override:
        return item
    title = str(override.get("title") or "").strip()
    author = str(override.get("author") or "").strip()
    series = str(override.get("series") or "").strip()
    sequence = str(override.get("series_index") or "").strip()
    summary = str(override.get("summary") or "").strip()
    cover = str(override.get("cover_url") or "").strip()

    if title:
        item["title"] = title
    if author:
        item["author"] = author
    if summary:
        item["description"] = summary
    if cover and (
        cover.startswith("http://")
        or cover.startswith("https://")
        or cover.startswith("/")
    ):
        item["coverUrl"] = cover

    if sequence:
        item["sequence"] = sequence
        try:
            num = float(sequence)
            item["volumeNumber"] = int(num) if num.is_integer() else num
        except (TypeError, ValueError):
            pass

    if series and series.casefold() != (title or str(item.get("title") or "")).casefold():
        item["seriesName"] = series
        item["series"] = [
            {"name": series, "sequence": sequence or str(item.get("sequence") or "")}
        ]
    elif multi_volume and series:
        item["seriesName"] = series
        item["series"] = [
            {"name": series, "sequence": sequence or str(item.get("sequence") or "")}
        ]

    item["metadataOverride"] = True
    return item


def load_applied_ebook_meta(staging: Path) -> EbookMeta | None:
    """Load manually applied ebook metadata from staging, if present."""
    if not staging:
        return None
    data = _read_applied_ebook_raw(Path(staging))
    if not data:
        return None
    raw = data.get("meta") if isinstance(data.get("meta"), dict) else data
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    author = str(raw.get("author") or "").strip()
    if not title:
        return None
    try:
        score = float(raw.get("score") or 1.0)
    except (TypeError, ValueError):
        score = 1.0
    source = str(raw.get("source") or "manual").strip() or "manual"
    label = _SOURCE_LABELS.get(source, source.replace("_", " ").title())
    return EbookMeta(
        title=title,
        author=author or "Unknown",
        series=(str(raw.get("series") or "").strip() or None),
        series_index=(str(raw.get("series_index") or "").strip() or None),
        edition=(str(raw.get("edition") or "").strip() or None),
        isbn13=(str(raw.get("isbn13") or "").strip() or None),
        isbn10=(str(raw.get("isbn10") or "").strip() or None),
        score=score,
        source=source,
        cover_url=(str(raw.get("cover_url") or "").strip() or None),
        ambiguous=False,
        reason=str(raw.get("reason") or f"Manual {label} match").strip(),
    )


def iter_applied_ebook_override_dirs(ebook_root: Path | None = None) -> list[Path]:
    """Find folders under the ebook library that contain ``ebook_applied.json``."""
    from app.config import get_settings

    root = Path(ebook_root or get_settings().ebook_dir)
    if not root.is_dir():
        return []
    skip = {"unorganized", "caltrash"}
    found: list[Path] = []
    for path in root.rglob(APPLIED_META_FILENAME):
        try:
            if any(part.lower() in skip for part in path.parts):
                continue
        except Exception:
            continue
        if path.is_file():
            found.append(path.parent)
    return found


async def resync_applied_ebook_overrides_to_kavita() -> dict[str, Any]:
    """Re-pin Kavita series locks from on-disk ``ebook_applied.json`` after a scan.

    Mirrors ABS ``sync_book_dir_metadata_to_abs``: scan may re-read file tags, then
    we push saved identity back. File-scoped: multi-volume series keep the series
    name (never stamp one volume title onto siblings).
    """
    from app.services import kavita

    dirs = iter_applied_ebook_override_dirs()
    out: dict[str, Any] = {"scanned": len(dirs), "updated": 0, "skipped": 0, "errors": 0, "resolved": 0}
    # Group by series_id when known (or resolved from on-disk ebook path).
    by_series: dict[int, list[dict[str, Any]]] = {}
    for folder in dirs:
        data = _read_applied_ebook_raw(folder)
        if not data:
            out["skipped"] += 1
            continue
        sid = data.get("kavita_series_id")
        try:
            sid_i = int(sid) if sid is not None else None
        except (TypeError, ValueError):
            sid_i = None
        if sid_i is None:
            # Sweep organize historically omitted series ids — resolve via primary ebook path.
            try:
                from app.services.ebook_pipeline import pick_primary_ebook

                primary = pick_primary_ebook(folder)
                if primary is not None:
                    sid_i = await kavita.find_series_id_for_ebook_path(primary)
                    if sid_i is not None:
                        from app.services.ebook_pipeline import ebook_series_paths_coherent

                        paths = await kavita.get_series_local_file_paths(sid_i)
                        if not ebook_series_paths_coherent(primary, paths):
                            sid_i = None
                        else:
                            ov_tmp = load_applied_ebook_override(folder)
                            meta_tmp = load_applied_ebook_meta(folder)
                            if meta_tmp is not None:
                                write_applied_ebook_meta(
                                    folder,
                                    meta_tmp,
                                    summary=(ov_tmp or {}).get("summary"),
                                    manually_applied=bool(data.get("manually_applied")),
                                    kavita_series_id=sid_i,
                                    kavita_chapter_id=data.get("kavita_chapter_id"),
                                )
                            out["resolved"] += 1
            except Exception as resolve_err:
                logger.debug("Could not resolve Kavita series for %s: %s", folder, resolve_err)
        if sid_i is None:
            out["skipped"] += 1
            continue
        ov = load_applied_ebook_override(folder)
        if not ov:
            out["skipped"] += 1
            continue
        ov = {**ov, "_folder": str(folder)}
        by_series.setdefault(sid_i, []).append(ov)

    from app.services.ebook_pipeline import ebook_series_paths_coherent, pick_primary_ebook
    from app.utils.book_series import is_corrupt_metadata_value

    for sid, overrides in by_series.items():
        try:
            paths = await kavita.get_series_local_file_paths(sid)
            coherent = True
            for ov in overrides:
                folder = Path(ov.get("_folder") or "")
                primary = pick_primary_ebook(folder) if folder.is_dir() else None
                if primary is None or not ebook_series_paths_coherent(primary, paths):
                    coherent = False
                    break
            if not coherent:
                logger.warning(
                    "Skipping Kavita resync for series %s - incoherent (%s files)",
                    sid,
                    len(paths),
                )
                out["skipped"] += 1
                continue
            multi = len(paths) > 1
            series_name = next((o.get("series") for o in overrides if o.get("series")), "") or ""
            if is_corrupt_metadata_value(series_name):
                series_name = ""
            title = overrides[0].get("title") or ""
            if is_corrupt_metadata_value(title):
                title = ""
            author = next((o.get("author") for o in overrides if o.get("author")), "") or ""
            if is_corrupt_metadata_value(author):
                author = ""
            summary = next((o.get("summary") for o in overrides if o.get("summary")), "") or None
            if is_corrupt_metadata_value(summary):
                summary = None
            name = series_name if multi else (title or series_name)
            if not name:
                out["skipped"] += 1
                continue
            ok = await kavita.update_series_identity(
                sid,
                name=name,
                author=author or None,
                summary=summary,
            )
            cover_url = next((o.get("cover_url") for o in overrides if o.get("cover_url")), "") or ""
            if (not multi) and cover_url.startswith(("http://", "https://")):
                try:
                    await kavita.set_series_cover_from_url(sid, cover_url)
                except Exception as cover_err:
                    logger.warning("Ebook cover resync for series %s failed: %s", sid, cover_err)
            if ok:
                out["updated"] += 1
            else:
                out["errors"] += 1
        except Exception as e:
            logger.warning("Ebook override resync for series %s failed: %s", sid, e)
            out["errors"] += 1

    if out["updated"]:
        kavita.invalidate_cache()
    return out


def _normalize_source(raw: Any) -> str:
    src = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if src in ("ol", "openlibrary", "open_library", "ol_catalog"):
        return "open_library"
    if src in ("gb", "gbooks", "google", "google_books", "googlebooks"):
        return "google_books"
    if src in ("hc", "hardcover"):
        return "hardcover"
    return src or "manual"


def selected_result_to_ebook_meta(selected: dict[str, Any]) -> EbookMeta:
    """Map a search card payload (Hardcover / Google Books / Open Library) into EbookMeta."""
    if not isinstance(selected, dict) or not selected:
        raise EbookQuickReviewError("selected_result is required")

    title = str(selected.get("title") or "").strip()
    if not title:
        raise EbookQuickReviewError("selected_result.title is required")

    authors = selected.get("authors")
    if isinstance(authors, list):
        author = ", ".join(str(a).strip() for a in authors if str(a).strip())
    else:
        author = str(selected.get("author") or "").strip()
    if not author:
        author = "Unknown"

    series = str(selected.get("series") or selected.get("seriesName") or "").strip() or None
    sequence = selected.get("sequence")
    if sequence is None:
        sequence = selected.get("seriesBookNumber")
    series_index = str(sequence).strip() if sequence is not None and str(sequence).strip() else None

    cover = (
        str(selected.get("cover_url") or selected.get("coverUrl") or "").strip() or None
    )
    try:
        score = float(selected.get("score") if selected.get("score") is not None else 1.0)
    except (TypeError, ValueError):
        score = 1.0

    source = _normalize_source(selected.get("source"))
    # Infer provider from volume id when source omitted (older clients).
    vid = str(selected.get("id") or selected.get("volumeId") or "").strip()
    if source in ("", "manual") and vid.upper().startswith("OL:"):
        source = "open_library"
    if source not in ("hardcover", "google_books", "open_library"):
        if selected.get("hardcover_id") or selected.get("hardcoverId"):
            source = "hardcover"
        elif vid.upper().startswith("OL:"):
            source = "open_library"
        elif vid and not vid.upper().startswith(("OL:", "ISBN:", "HC:")):
            # Bare Google Books volume ids (e.g. zyTCAlFPjgYC).
            source = "google_books"
        else:
            source = "manual"

    label = _SOURCE_LABELS.get(source, source.replace("_", " ").title())
    return EbookMeta(
        title=title,
        author=author,
        series=series,
        series_index=series_index,
        isbn13=(str(selected.get("isbn13") or "").strip() or None),
        isbn10=(str(selected.get("isbn10") or "").strip() or None),
        score=min(max(score, 0.0), 1.0) if score <= 1 else 1.0,
        source=source,
        cover_url=cover,
        ambiguous=False,
        reason=f"Manual {label} match",
    )


def _norm_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _result_dedupe_key(result: dict[str, Any]) -> str:
    isbn13 = re.sub(r"\D", "", str(result.get("isbn13") or ""))
    if len(isbn13) == 13:
        return f"isbn:{isbn13}"
    isbn10 = re.sub(r"[^0-9Xx]", "", str(result.get("isbn10") or ""))
    if len(isbn10) == 10:
        return f"isbn10:{isbn10.upper()}"
    authors = result.get("authors") if isinstance(result.get("authors"), list) else []
    first = _norm_text(str(authors[0]) if authors else "")
    title = _norm_text(str(result.get("title") or ""))
    if title:
        return f"ta:{title}:{first}"
    return f"id:{result.get('id') or result.get('hardcover_id') or ''}"


def _source_rank(source: str) -> int:
    # Prefer Hardcover (series/sequence), then Google Books, then Open Library.
    if source == "hardcover":
        return 3
    if source == "google_books":
        return 2
    if source == "open_library":
        return 1
    return 0


def _merge_search_results(
    batches: list[list[dict[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Merge provider result lists, dedupe, prefer Hardcover > GB > OL, keep best cover/series."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for batch in batches:
        for item in batch:
            if not item or not str(item.get("title") or "").strip():
                continue
            key = _result_dedupe_key(item)
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(item)
                order.append(key)
                continue

            # Prefer Hardcover identity; fill gaps from the other source.
            keep = existing
            other = item
            if _source_rank(str(item.get("source") or "")) > _source_rank(
                str(existing.get("source") or "")
            ):
                keep = dict(item)
                other = existing
                merged[key] = keep

            for field in (
                "cover_url",
                "isbn13",
                "isbn10",
                "series",
                "sequence",
                "subtitle",
                "summary",
                "publisher",
                "year",
                "info_link",
            ):
                if not str(keep.get(field) or "").strip() and str(other.get(field) or "").strip():
                    keep[field] = other[field]
            try:
                keep_score = float(keep.get("score") or 0)
            except (TypeError, ValueError):
                keep_score = 0.0
            try:
                other_score = float(other.get("score") or 0)
            except (TypeError, ValueError):
                other_score = 0.0
            keep["score"] = max(keep_score, other_score)

    ranked = [merged[k] for k in order]
    ranked.sort(
        key=lambda r: (
            float(r.get("score") or 0),
            _source_rank(str(r.get("source") or "")),
            1 if str(r.get("cover_url") or "").strip() else 0,
        ),
        reverse=True,
    )
    return ranked[: max(1, limit)]


def _hc_hit_to_result(hit: dict[str, Any], *, query_title: str = "") -> dict[str, Any]:
    authors = hit.get("authors") if isinstance(hit.get("authors"), list) else []
    title = str(hit.get("title") or "").strip()
    cover = str(hit.get("coverUrl") or hit.get("cover_url") or "").strip()
    series = str(hit.get("seriesName") or "").strip()
    sequence = hit.get("seriesBookNumber")
    score = None
    if query_title and title:
        score = round(title_similarity(query_title, title), 3)
    return {
        "id": hit.get("id") or hit.get("volumeId"),
        "hardcover_id": hit.get("hardcoverId"),
        "hardcover_slug": hit.get("hardcoverSlug"),
        "title": title,
        "subtitle": str(hit.get("subtitle") or "").strip(),
        "authors": [str(a).strip() for a in authors if str(a).strip()],
        "series": series,
        "sequence": str(sequence).strip() if sequence is not None and str(sequence).strip() else "",
        "year": str(hit.get("publishedDate") or "").strip(),
        "isbn13": str(hit.get("isbn13") or "").strip(),
        "isbn10": str(hit.get("isbn10") or "").strip(),
        "cover_url": cover,
        "summary": str(hit.get("description") or "").strip(),
        "publisher": str(hit.get("publisher") or "").strip(),
        "language": str(hit.get("language") or "").strip(),
        "score": score,
        "info_link": str(hit.get("infoLink") or hit.get("previewLink") or "").strip(),
        "source": "hardcover",
    }


def _catalog_hit_to_result(
    hit: dict[str, Any],
    *,
    query_title: str = "",
    source: str = "open_library",
) -> dict[str, Any]:
    """Normalize OL / Google Books volume cards into matcher results."""
    authors = hit.get("authors") if isinstance(hit.get("authors"), list) else []
    title = str(hit.get("title") or "").strip()
    cover = str(
        hit.get("coverUrl")
        or hit.get("cover_url")
        or hit.get("coverUrlLarge")
        or ""
    ).strip()
    series = str(hit.get("seriesName") or hit.get("series") or "").strip()
    sequence = hit.get("seriesBookNumber")
    if sequence is None:
        sequence = hit.get("series_index") or hit.get("sequence")
    score = None
    if query_title and title:
        score = round(title_similarity(query_title, title), 3)
    vid = str(hit.get("id") or hit.get("volumeId") or "").strip()
    info = str(hit.get("infoLink") or hit.get("previewLink") or "").strip()
    if not info and vid.upper().startswith("OL:"):
        ol_key = vid[3:]
        if not ol_key.startswith("/"):
            ol_key = f"/{ol_key}" if ol_key else ""
        if ol_key:
            info = f"https://openlibrary.org{ol_key}"
    return {
        "id": vid or None,
        "title": title,
        "subtitle": str(hit.get("subtitle") or "").strip(),
        "authors": [str(a).strip() for a in authors if str(a).strip()],
        "series": series,
        "sequence": str(sequence).strip() if sequence is not None and str(sequence).strip() else "",
        "year": str(hit.get("publishedDate") or hit.get("year") or "").strip(),
        "isbn13": str(hit.get("isbn13") or "").strip(),
        "isbn10": str(hit.get("isbn10") or "").strip(),
        "cover_url": cover,
        "summary": str(hit.get("description") or "").strip(),
        "publisher": str(hit.get("publisher") or "").strip(),
        "language": str(hit.get("language") or "").strip(),
        "score": score,
        "info_link": info,
        "source": source,
    }


def _ol_hit_to_result(hit: dict[str, Any], *, query_title: str = "") -> dict[str, Any]:
    return _catalog_hit_to_result(hit, query_title=query_title, source="open_library")


def _gb_hit_to_result(hit: dict[str, Any], *, query_title: str = "") -> dict[str, Any]:
    return _catalog_hit_to_result(hit, query_title=query_title, source="google_books")


async def load_ebook_quick_review(req: DownloadRequest) -> dict[str, Any]:
    """Load staging targets + search clues for ebook metadata matching."""
    if (req.media_type or "") != "ebook":
        raise EbookQuickReviewError("Ebook Quick Review is ebook-only")
    if req.status not in ("quarantined", "metadata_forge", "folder_forge"):
        raise EbookQuickReviewError(
            f"Cannot review ebook metadata while request status is '{req.status}'"
        )

    staging = resolve_staging_dir(req.staging_path or "")
    targets = list_ebook_staging_targets(staging)
    if not targets:
        raise EbookQuickReviewError("No ebook files found in staging")

    folder_hint = _folder_title_hint(staging)
    title = clean_catalog_title(req.title or "") or (req.title or "").strip() or folder_hint
    title = clean_ebook_search_title(title) or title
    author = (req.author or "").strip()
    if author.lower() == "unknown":
        author = ""

    query = f"{title} {author}".strip()
    applied = load_applied_ebook_meta(staging)
    primary = pick_primary_ebook(staging)

    return {
        "request_id": req.id,
        "title": req.title,
        "author": req.author,
        "status": req.status,
        "quarantine_reason": getattr(req, "quarantine_reason", None),
        "staging_path": staging_path_for_libraforge(staging),
        "targets": targets,
        "selected_relative_path": targets[0].get("relative_path") or "",
        "primary_ebook": primary.name if primary else None,
        "queries": [query] if query else [],
        "clues": {
            "query": query,
            "title": title,
            "author": author,
            "series": (applied.series if applied else "") or "",
            "sequence": (applied.series_index if applied else "") or "",
            "cover_url": (applied.cover_url if applied else "")
            or (getattr(req, "cover_url", None) or "")
            or "",
        },
        "metadata": {
            "title": (applied.title if applied else title) or "",
            "author": (applied.author if applied else author) or "",
            "series": (applied.series if applied else "") or "",
            "sequence": (applied.series_index if applied else "") or "",
            "isbn13": (applied.isbn13 if applied else "") or "",
            "isbn10": (applied.isbn10 if applied else "") or "",
            "cover_url": (applied.cover_url if applied else "")
            or (getattr(req, "cover_url", None) or "")
            or "",
            "source": (applied.source if applied else "") or "",
        },
        "already_applied": applied is not None,
        "provider": "hardcover+google_books+open_library",
        "providers": ["hardcover", "google_books", "open_library"],
    }


async def search_ebook_metadata_candidates(
    *,
    query: str = "",
    title: str = "",
    author: str = "",
    limit: int = 12,
) -> dict[str, Any]:
    """Search Hardcover + Google Books + Open Library (quarantine + library editors)."""
    from app.config import get_settings
    from app.services import google_books, hardcover

    t = clean_ebook_search_title((title or "").strip()) or (title or "").strip()
    a = (author or "").strip()
    q = clean_ebook_search_title((query or "").strip()) or (query or "").strip()
    if not q:
        q = f"{t} {a}".strip()
    else:
        # Prefer cleaned title words even when a raw query was provided.
        q = clean_ebook_search_title(q) or q
    if len(q) < 2:
        raise EbookQuickReviewError("Search query is required")

    lim = min(max(1, limit), 25)
    query_title = t or q
    errors: list[str] = []
    providers_used: list[str] = []
    hc_results: list[dict[str, Any]] = []
    gb_results: list[dict[str, Any]] = []
    ol_results: list[dict[str, Any]] = []
    settings = get_settings()

    if await hardcover.get_api_key():
        try:
            hits = await hardcover.search_books(q, limit=lim)
            hc_results = [
                _hc_hit_to_result(h, query_title=query_title) for h in (hits or []) if h
            ]
            providers_used.append("hardcover")
        except Exception as e:
            logger.warning("Hardcover ebook review search failed: %s", e)
            errors.append(f"Hardcover: {e}")
    else:
        errors.append("Hardcover API key is not configured")

    if (settings.google_books_api_key or "").strip():
        try:
            gb_data = await google_books.search_google_books(q, max_results=lim)
            books = (gb_data or {}).get("books") if isinstance(gb_data, dict) else None
            if isinstance(books, list):
                gb_results = [
                    _gb_hit_to_result(b, query_title=query_title) for b in books if b
                ]
                if gb_results:
                    providers_used.append("google_books")
        except Exception as e:
            logger.warning("Google Books ebook review search failed: %s", e)
            errors.append(f"Google Books: {e}")
    else:
        errors.append("Google Books API key is not configured")

    try:
        ol_books = await google_books.search_open_library(q, limit=lim)
        if isinstance(ol_books, list):
            ol_results = [
                _ol_hit_to_result(b, query_title=query_title) for b in ol_books if b
            ]
            if ol_results:
                providers_used.append("open_library")
    except Exception as e:
        logger.warning("Open Library ebook review search failed: %s", e)
        errors.append(f"Open Library: {e}")

    results = _merge_search_results([hc_results, gb_results, ol_results], limit=lim)

    if not results and errors and not (hc_results or gb_results or ol_results):
        raise EbookQuickReviewError(
            "; ".join(errors)
            if errors
            else "No metadata matches found (Hardcover / Google Books / Open Library empty)"
        )

    return {
        "ok": True,
        "query": q,
        "provider": "hardcover+google_books+open_library",
        "providers": providers_used
        or ["hardcover", "google_books", "open_library"],
        "results": results,
        "queries": [q],
        "errors": errors,
    }


async def search_ebook_quick_review(
    req: DownloadRequest,
    *,
    query: str = "",
    title: str = "",
    author: str = "",
    limit: int = 12,
) -> dict[str, Any]:
    """Search Hardcover, Google Books, and Open Library for ebook metadata candidates."""
    if (req.media_type or "") != "ebook":
        raise EbookQuickReviewError("Ebook Quick Review search is ebook-only")

    resolve_staging_dir(req.staging_path or "")
    data = await search_ebook_metadata_candidates(
        query=query,
        title=title,
        author=author,
        limit=limit,
    )
    return {**data, "request_id": req.id}


async def apply_ebook_quick_review(
    req: DownloadRequest,
    *,
    selected_result: dict[str, Any],
) -> dict[str, Any]:
    """Apply selected Hardcover/Google Books/Open Library metadata to staging + request display."""
    if (req.media_type or "") != "ebook":
        raise EbookQuickReviewError("Ebook Quick Review apply is ebook-only")
    if req.status not in ("quarantined", "metadata_forge"):
        raise EbookQuickReviewError(
            f"Cannot apply ebook metadata while request status is '{req.status}'"
        )

    staging = resolve_staging_dir(req.staging_path or "")
    if not list_ebook_staging_targets(staging):
        raise EbookQuickReviewError("No ebook files found in staging")

    meta = selected_result_to_ebook_meta(selected_result)
    meta.score = max(meta.score, 0.95)
    meta.ambiguous = False
    label = _SOURCE_LABELS.get(meta.source, meta.source.replace("_", " ").title())
    if not meta.reason:
        meta.reason = f"Manual {label} match"

    primary = pick_primary_ebook(staging)
    if primary:
        primary = rename_ebook_to_metadata_title(primary, meta.title or primary.stem)

    applied_path = write_applied_ebook_meta(staging, meta)

    embedded = False
    if primary:
        try:
            embedded = await embed_ebook_metadata(primary, meta)
        except Exception as e:
            logger.warning(
                "Could not embed ebook metadata for request %s: %s", req.id, e
            )

    from app.services.forge_pipeline import refresh_request_display_metadata

    try:
        await refresh_request_display_metadata(
            req.id,
            staging,
            title=meta.title or None,
            author=meta.author or None,
            cover_url=meta.cover_url or None,
        )
    except Exception as e:
        logger.warning(
            "Could not refresh request display after ebook apply for %s: %s",
            req.id,
            e,
        )

    return {
        "ok": True,
        "request_id": req.id,
        "applied": True,
        "embedded": embedded,
        "applied_meta_path": staging_path_for_libraforge(applied_path),
        "primary_ebook": primary.name if primary else None,
        "metadata_preview": {
            "title": meta.title,
            "author": meta.author,
            "series": meta.series,
            "sequence": meta.series_index,
            "isbn13": meta.isbn13,
            "isbn10": meta.isbn10,
            "cover_url": meta.cover_url,
            "source": meta.source,
        },
        "cover_url": meta.cover_url,
    }
