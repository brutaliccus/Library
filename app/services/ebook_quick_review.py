"""In-app ebook metadata matcher: Hardcover search -> select -> apply.

Mirrors audiobook Quick Review's load/search/apply UX without LibraForge.
Applied matches are persisted as ``ebook_applied.json`` so Continue organize
uses the admin-selected identity instead of re-running auto-identify.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.models import DownloadRequest
from app.services.ebook_pipeline import (
    EbookMeta,
    _collect_ebooks,
    embed_ebook_metadata,
    pick_primary_ebook,
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


def write_applied_ebook_meta(staging: Path, meta: EbookMeta) -> Path:
    """Persist admin-selected ebook identity for Continue organize."""
    staging = staging.resolve()
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / APPLIED_META_FILENAME
    payload = {
        "applied": True,
        "manually_applied": True,
        "source": meta.source or "hardcover",
        "meta": {
            "title": meta.title,
            "author": meta.author,
            "series": meta.series,
            "series_index": meta.series_index,
            "edition": meta.edition,
            "isbn13": meta.isbn13,
            "isbn10": meta.isbn10,
            "score": meta.score,
            "source": meta.source,
            "cover_url": meta.cover_url,
            "reason": meta.reason,
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_applied_ebook_meta(staging: Path) -> EbookMeta | None:
    """Load manually applied ebook metadata from staging, if present."""
    if not staging or not staging.is_dir():
        return None
    path = staging / APPLIED_META_FILENAME
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
    raw = data.get("meta") if isinstance(data.get("meta"), dict) else data
    title = str(raw.get("title") or "").strip()
    author = str(raw.get("author") or "").strip()
    if not title:
        return None
    try:
        score = float(raw.get("score") or 1.0)
    except (TypeError, ValueError):
        score = 1.0
    return EbookMeta(
        title=title,
        author=author or "Unknown",
        series=(str(raw.get("series") or "").strip() or None),
        series_index=(str(raw.get("series_index") or "").strip() or None),
        edition=(str(raw.get("edition") or "").strip() or None),
        isbn13=(str(raw.get("isbn13") or "").strip() or None),
        isbn10=(str(raw.get("isbn10") or "").strip() or None),
        score=score,
        source=str(raw.get("source") or "manual").strip() or "manual",
        cover_url=(str(raw.get("cover_url") or "").strip() or None),
        ambiguous=False,
        reason=str(raw.get("reason") or "Manual Hardcover match").strip(),
    )


def selected_result_to_ebook_meta(selected: dict[str, Any]) -> EbookMeta:
    """Map a Hardcover search card payload into EbookMeta."""
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

    return EbookMeta(
        title=title,
        author=author,
        series=series,
        series_index=series_index,
        isbn13=(str(selected.get("isbn13") or "").strip() or None),
        isbn10=(str(selected.get("isbn10") or "").strip() or None),
        score=min(max(score, 0.0), 1.0) if score <= 1 else 1.0,
        source="hardcover",
        cover_url=cover,
        ambiguous=False,
        reason="Manual Hardcover match",
    )


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
        "provider": "hardcover",
    }


async def search_ebook_quick_review(
    req: DownloadRequest,
    *,
    query: str = "",
    title: str = "",
    author: str = "",
    limit: int = 12,
) -> dict[str, Any]:
    """Search Hardcover for ebook metadata candidates."""
    if (req.media_type or "") != "ebook":
        raise EbookQuickReviewError("Ebook Quick Review search is ebook-only")

    from app.services import hardcover

    resolve_staging_dir(req.staging_path or "")

    q = (query or "").strip()
    t = (title or "").strip()
    a = (author or "").strip()
    if not q:
        q = f"{t} {a}".strip()
    if len(q) < 2:
        raise EbookQuickReviewError("Search query is required")

    if not await hardcover.get_api_key():
        raise EbookQuickReviewError(
            "Hardcover API key is not configured (Admin -> Integrations)"
        )

    try:
        hits = await hardcover.search_books(q, limit=min(max(1, limit), 25))
    except Exception as e:
        raise EbookQuickReviewError(f"Hardcover search failed: {e}") from e

    results = [_hc_hit_to_result(h, query_title=t or q) for h in (hits or []) if h]
    if t:
        results.sort(key=lambda r: float(r.get("score") or 0), reverse=True)

    return {
        "ok": True,
        "request_id": req.id,
        "query": q,
        "provider": "hardcover",
        "results": results,
        "queries": [q],
    }


async def apply_ebook_quick_review(
    req: DownloadRequest,
    *,
    selected_result: dict[str, Any],
) -> dict[str, Any]:
    """Apply selected Hardcover metadata to staging + request display."""
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
    meta.source = "hardcover"
    meta.reason = "Manual Hardcover match"
    meta.ambiguous = False

    applied_path = write_applied_ebook_meta(staging, meta)

    primary = pick_primary_ebook(staging)
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
