"""In-app Quick Admin Review: load/search/apply metadata via LibraForge proxies.

Keeps path operations inside the request staging tree and prefers catalog /
folder titles over junk chapter filenames (e.g. Timeline over Tape1).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.models import DownloadRequest
from app.services import libraforge
from app.services.forge_pipeline import (
    _audio_parent_dirs,
    _collect_audio,
    clean_catalog_title,
    resolve_staging_dir,
    safe_path_under_staging,
    staging_has_applied_metadata,
    staging_path_for_libraforge,
)

logger = logging.getLogger(__name__)

_JUNK_TITLE_RE = re.compile(
    r"^(?:tape|track|disc|disk|cd|chapter|part|pt|file|audio|track)\s*[-_.]?\s*\d+$"
    r"|^(?:chapter|part|disc|disk|cd|track)\s+\d+$"
    r"|^\d{1,3}$",
    re.IGNORECASE,
)
_REQ_FOLDER_RE = re.compile(r"^req_\d+_(.+)$", re.IGNORECASE)
_SPECIAL_PROVIDER_HINTS = (
    ("graphicaudio", "graphicaudio"),
    ("soundbooth", "soundbooththeater"),
)


class QuickReviewError(ValueError):
    """User-facing quick review failure (bad path / status / empty staging)."""


def _looks_like_junk_title(value: str) -> bool:
    t = (value or "").strip()
    if not t:
        return True
    stem = Path(t).stem if "." in t and " " not in t.split(".")[-1] else t
    return bool(_JUNK_TITLE_RE.match(stem.strip()))


def _folder_title_hint(folder: Path) -> str:
    name = folder.name.strip()
    m = _REQ_FOLDER_RE.match(name)
    if m:
        name = m.group(1).replace("_", " ").strip()
    return clean_catalog_title(name) or name


def _provider_hint_from_meta(meta: dict[str, Any]) -> str | None:
    blob = " ".join(
        str(meta.get(k) or "")
        for k in ("series", "publisher", "title", "author", "narrator")
    ).lower()
    for needle, provider in _SPECIAL_PROVIDER_HINTS:
        if needle in blob:
            return provider
    return None


def _build_query(*, title: str, author: str, series: str = "", sequence: str = "") -> str:
    parts: list[str] = []
    t = (title or "").strip()
    a = (author or "").strip()
    s = (series or "").strip()
    n = (sequence or "").strip()
    if t and a:
        parts.append(f"{t} {a}")
    elif t:
        parts.append(t)
    elif a and s:
        parts.append(f"{a} {s}" + (f" Book {n}" if n else ""))
    elif a:
        parts.append(a)
    return parts[0] if parts else ""


def list_staging_targets(staging: Path) -> list[dict[str, Any]]:
    """Book folders (or single files) under staging suitable for Manual Review load."""
    staging_res = staging.resolve()
    parents = _audio_parent_dirs(staging_res)
    targets: list[dict[str, Any]] = []
    if not parents:
        return targets

    for parent in parents:
        try:
            rel = parent.relative_to(staging_res)
        except ValueError:
            continue
        rel_posix = rel.as_posix() if str(rel) != "." else ""
        lf_path = staging_path_for_libraforge(parent)
        audio = _collect_audio(parent)
        targets.append(
            {
                "relative_path": rel_posix,
                "path": lf_path,
                "display_name": parent.name if rel_posix else staging_res.name,
                "file_count": len(audio),
                "is_grouped": len(audio) > 1,
            }
        )
    return targets


def resolve_target_path(staging: Path, relative_path: str | None) -> tuple[Path, str]:
    """Return (local Path, LibraForge Docker path) under staging."""
    staging_res = staging.resolve()
    rel = (relative_path or "").strip().replace("\\", "/")
    if not rel or rel in {".", "./"}:
        target = staging_res
    else:
        target = safe_path_under_staging(staging_res, rel)
        if not target.exists():
            raise QuickReviewError(f"Target path not found in staging: {rel}")
    return target, staging_path_for_libraforge(target)


def merge_clues_with_catalog(
    loaded: dict[str, Any],
    *,
    request_title: str | None,
    request_author: str | None,
    folder_hint: str = "",
) -> dict[str, Any]:
    """Prefer catalog / folder title over junk chapter filenames (Tape1, etc.)."""
    meta = dict(loaded.get("metadata") or {})
    catalog_title = clean_catalog_title(request_title or "") or (request_title or "").strip()
    catalog_author = (request_author or "").strip()
    if catalog_author.lower() == "unknown":
        catalog_author = ""

    loaded_title = str(meta.get("title") or meta.get("raw_title") or "").strip()
    loaded_author = str(meta.get("author") or "").strip()

    if loaded_title and not _looks_like_junk_title(loaded_title):
        title = loaded_title
    elif folder_hint and not _looks_like_junk_title(folder_hint):
        title = folder_hint
    elif catalog_title:
        title = catalog_title
    else:
        title = loaded_title or folder_hint or catalog_title

    if _looks_like_junk_title(title):
        title = folder_hint or catalog_title or title

    author = loaded_author or catalog_author
    series = str(meta.get("series") or "").strip()
    sequence = str(meta.get("sequence") or "").strip()
    narrator = str(meta.get("narrator") or "").strip()

    queries = [str(q) for q in (loaded.get("queries") or []) if str(q).strip()]
    preferred = _build_query(title=title, author=author, series=series, sequence=sequence)
    if preferred:
        rest = [q for q in queries if q.lower() != preferred.lower()]
        queries = [preferred, *rest]

    clues = {
        "query": queries[0] if queries else preferred,
        "title": title,
        "author": author,
        "series": series,
        "sequence": sequence,
        "narrator": narrator,
        "subtitle": str(meta.get("subtitle") or "").strip(),
        "year": str(meta.get("year") or "").strip(),
        "asin": str(meta.get("asin") or "").strip(),
        "publisher": str(meta.get("publisher") or "").strip(),
        "summary": str(meta.get("summary") or "").strip(),
        "cover_url": str(meta.get("cover_url") or "").strip(),
        "local_duration_minutes": meta.get("local_duration_minutes"),
    }
    return {
        **loaded,
        "metadata": {**meta, **{k: v for k, v in clues.items() if k != "query"}},
        "queries": queries,
        "clues": clues,
        "provider_hint": _provider_hint_from_meta({**meta, **clues}),
    }


async def load_quick_review(
    req: DownloadRequest,
    *,
    relative_path: str | None = None,
) -> dict[str, Any]:
    """Load staging targets + Manual Review clues for Quick Admin Review."""
    if (req.media_type or "") == "ebook":
        raise QuickReviewError("Quick Review metadata is audiobook-only (ebooks use Continue)")

    staging = resolve_staging_dir(req.staging_path or "")
    targets = list_staging_targets(staging)
    if not targets:
        raise QuickReviewError("No audio files found in staging")

    chosen_rel = (relative_path or "").strip()
    if not chosen_rel:
        chosen_rel = targets[0].get("relative_path") or ""

    target_local, lf_path = resolve_target_path(staging, chosen_rel or None)
    folder_hint = _folder_title_hint(target_local if target_local.is_dir() else target_local.parent)

    try:
        loaded = await libraforge.manual_review_load(lf_path)
    except libraforge.LibraForgeError as e:
        audio = _collect_audio(target_local) if target_local.is_dir() else [target_local]
        if not audio:
            raise QuickReviewError(str(e)) from e
        try:
            loaded = await libraforge.manual_review_load(
                staging_path_for_libraforge(audio[0])
            )
            lf_path = staging_path_for_libraforge(audio[0])
        except libraforge.LibraForgeError as e2:
            raise QuickReviewError(str(e2)) from e2

    merged = merge_clues_with_catalog(
        loaded,
        request_title=req.title,
        request_author=req.author,
        folder_hint=folder_hint,
    )

    review_url = libraforge.public_manual_review_url() or None
    applied = staging_has_applied_metadata(staging)

    return {
        "request_id": req.id,
        "title": req.title,
        "author": req.author,
        "status": req.status,
        "quarantine_reason": getattr(req, "quarantine_reason", None),
        "staging_path": staging_path_for_libraforge(staging),
        "manual_review_url": review_url,
        "targets": targets,
        "selected_relative_path": chosen_rel,
        "target_path": merged.get("display_path") or merged.get("path") or lf_path,
        "source_path": merged.get("source_path") or lf_path,
        "is_grouped": bool(merged.get("is_grouped")),
        "file_count": next(
            (t["file_count"] for t in targets if (t.get("relative_path") or "") == chosen_rel),
            targets[0]["file_count"] if targets else 1,
        ),
        "queries": merged.get("queries") or [],
        "clues": merged.get("clues") or {},
        "metadata": merged.get("metadata") or {},
        "provider_hint": merged.get("provider_hint"),
        "group_search": merged.get("group_search") or {},
        "already_applied": applied,
    }


async def search_quick_review(
    req: DownloadRequest,
    *,
    query: str,
    title: str = "",
    author: str = "",
    series: str = "",
    sequence: str = "",
    narrator: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Proxy Audible metadata search for the request's staging context."""
    if (req.media_type or "") == "ebook":
        raise QuickReviewError("Quick Review search is audiobook-only")
    resolve_staging_dir(req.staging_path or "")

    q = (query or "").strip()
    if not q:
        q = _build_query(title=title, author=author, series=series, sequence=sequence)
    if not q:
        raise QuickReviewError("Search query is required")

    metadata = {
        "title": (title or "").strip(),
        "author": (author or "").strip(),
        "series": (series or "").strip(),
        "sequence": (sequence or "").strip(),
        "narrator": (narrator or "").strip(),
    }
    try:
        data = await libraforge.manual_review_search(query=q, metadata=metadata, limit=limit)
    except libraforge.LibraForgeError as e:
        raise QuickReviewError(str(e)) from e

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        results = []
    return {
        "request_id": req.id,
        "queries": data.get("queries") if isinstance(data, dict) else [q],
        "results": results,
        "provider": "audible",
    }


def _cover_url_from_candidate(selected: dict[str, Any], edit_mode: str = "full") -> str:
    """Resolve cover URL from a Manual Review / search candidate payload."""
    by_mode = selected.get("chosen_metadata_by_mode") or {}
    for source in (
        (by_mode.get(edit_mode) or {}) if isinstance(by_mode, dict) else {},
        selected.get("chosen_metadata") or {},
        selected,
    ):
        if not isinstance(source, dict):
            continue
        val = str(source.get("cover_url") or source.get("cover") or "").strip()
        if val.startswith("http"):
            return val
    # Fall back to full-mode preview even when applying another mode for tags.
    if isinstance(by_mode, dict):
        full = by_mode.get("full") or {}
        if isinstance(full, dict):
            val = str(full.get("cover_url") or full.get("cover") or "").strip()
            if val.startswith("http"):
                return val
    return ""


def _enrich_selected_for_apply(
    selected_result: dict[str, Any],
    *,
    edit_mode: str,
    replace_cover: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ensure chosen metadata carries cover_url (and top-level URL) for LibraForge.

    LibraForge only embeds covers when ``edit_mode == "full"`` and
    ``replace_cover`` / ``cover_if_missing`` with a non-empty ``cover_url``
    on the chosen metadata blob — not the top-level candidate field alone.
    """
    selected = dict(selected_result)
    by_mode_raw = selected.get("chosen_metadata_by_mode") or {}
    by_mode: dict[str, Any] = dict(by_mode_raw) if isinstance(by_mode_raw, dict) else {}
    cover = _cover_url_from_candidate(selected, edit_mode)

    for mode_key in ("full", edit_mode):
        if mode_key not in {"full", "series_only"}:
            continue
        base = by_mode.get(mode_key) or selected.get("chosen_metadata") or {}
        if not isinstance(base, dict):
            base = {}
        entry = dict(base)
        if cover and mode_key == "full":
            entry["cover_url"] = cover
        elif mode_key == "series_only":
            # series_only never embeds covers in LibraForge writers
            entry.setdefault("cover_url", "")
        by_mode[mode_key] = entry

    if cover:
        selected["cover_url"] = cover
    selected["chosen_metadata_by_mode"] = by_mode
    full = by_mode.get("full") if isinstance(by_mode.get("full"), dict) else {}
    chosen = by_mode.get(edit_mode) if isinstance(by_mode.get(edit_mode), dict) else full
    if chosen:
        selected["chosen_metadata"] = dict(chosen)

    override: dict[str, Any] = {}
    if replace_cover and edit_mode == "full" and cover:
        override["cover_url"] = cover
    return selected, override


def resolve_apply_edit_mode(
    selected_result: dict[str, Any],
    *,
    edit_mode: str = "full",
    replace_cover: bool = True,
) -> str:
    """Pick apply edit_mode. Cover replace requires LibraForge ``full`` mode."""
    mode = (edit_mode or "full").strip()
    if mode not in {"full", "series_only"}:
        mode = "full"
    allowed_raw = selected_result.get("allowed_edit_modes") or []
    allowed = [str(m) for m in allowed_raw] if isinstance(allowed_raw, list) else []

    # Quick Review always requests replace_cover; LibraForge writers gate cover
    # embeds on edit_mode == "full". Prefer full whenever cover replace is on.
    if replace_cover and (not allowed or "full" in allowed):
        return "full"

    recommended = str(selected_result.get("recommended_edit_mode") or "").strip()
    if recommended in {"full", "series_only"} and (
        not allowed or recommended in allowed
    ):
        return recommended
    if allowed and mode not in allowed:
        return "full" if "full" in allowed else str(allowed[0])
    return mode


async def apply_quick_review(
    req: DownloadRequest,
    *,
    relative_path: str | None,
    selected_result: dict[str, Any],
    edit_mode: str = "full",
    replace_cover: bool = True,
) -> dict[str, Any]:
    """Apply selected metadata to staging via LibraForge Manual Review apply."""
    if (req.media_type or "") == "ebook":
        raise QuickReviewError("Quick Review apply is audiobook-only")
    if req.status not in ("quarantined", "metadata_forge"):
        raise QuickReviewError(
            f"Cannot apply metadata while request status is '{req.status}'"
        )
    if not isinstance(selected_result, dict) or not selected_result:
        raise QuickReviewError("selected_result is required")

    staging = resolve_staging_dir(req.staging_path or "")
    targets = list_staging_targets(staging)
    if not targets:
        raise QuickReviewError("No audio files found in staging")

    chosen_rel = (relative_path or "").strip()
    if not chosen_rel and targets:
        chosen_rel = targets[0].get("relative_path") or ""

    _target_local, lf_path = resolve_target_path(staging, chosen_rel or None)

    mode = resolve_apply_edit_mode(
        selected_result, edit_mode=edit_mode, replace_cover=replace_cover
    )
    enriched, metadata_override = _enrich_selected_for_apply(
        selected_result, edit_mode=mode, replace_cover=replace_cover
    )
    cover = _cover_url_from_candidate(enriched, mode)
    if replace_cover and mode == "full" and not cover:
        logger.warning(
            "Quick Review apply for request %s: replace_cover set but candidate has no cover_url",
            req.id,
        )

    try:
        result = await libraforge.manual_review_apply(
            path=lf_path,
            selected_result=enriched,
            edit_mode=mode,
            write_policy="overwrite",
            replace_cover=replace_cover,
            cover_if_missing=False,
            backup=False,
            metadata_override=metadata_override or None,
        )
    except libraforge.LibraForgeError as e:
        raise QuickReviewError(str(e)) from e

    applied = staging_has_applied_metadata(staging)
    if not applied:
        status = str(result.get("status") or "").lower()
        if status != "applied":
            raise QuickReviewError(
                "Metadata apply finished but no write evidence was found in staging"
            )
        logger.warning(
            "Quick Review apply for request %s returned applied without staging markers",
            req.id,
        )

    # Persist cover_url where Continue→M4B can recover it (ABS metadata.json
    # omits cover_url; nested libraforge paths are handled by cover_url_from_staging).
    preview = result.get("metadata_preview") if isinstance(result.get("metadata_preview"), dict) else {}
    applied_cover = str(
        (preview or {}).get("cover_url") or cover or ""
    ).strip()
    if applied_cover.startswith("http"):
        try:
            _stamp_cover_url_on_staging(staging, applied_cover)
        except OSError as e:
            logger.warning(
                "Could not stamp cover_url into staging for request %s: %s", req.id, e
            )

    return {
        "ok": True,
        "request_id": req.id,
        "applied": True,
        "write_evidence": applied,
        "target_path": result.get("target_path") or lf_path,
        "source_path": result.get("source_path"),
        "output_kind": result.get("output_kind"),
        "output_path": result.get("output_path"),
        "metadata_json_path": result.get("metadata_json_path"),
        "edit_mode": result.get("edit_mode") or mode,
        "write_policy": result.get("write_policy") or "overwrite",
        "metadata_preview": result.get("metadata_preview") or {},
        "cover_url": applied_cover or None,
        "warning": result.get("warning"),
        "manual_review_url": libraforge.public_manual_review_url() or None,
    }


def _stamp_cover_url_on_staging(staging: Path, cover_url: str) -> None:
    """Merge cover_url into existing metadata.json / libraforge.json for M4B handoff."""
    stamped = False
    for meta_path in staging.rglob("metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        if str(meta.get("cover_url") or "").strip() == cover_url:
            stamped = True
            continue
        meta["cover_url"] = cover_url
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        stamped = True
    for lf_path in staging.rglob("libraforge.json"):
        try:
            data = json.loads(lf_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        marker = data.get("marker")
        if isinstance(marker, dict):
            audible = marker.get("audible")
            if isinstance(audible, dict) and not str(audible.get("cover_url") or "").strip():
                audible["cover_url"] = cover_url
                changed = True
            elif not isinstance(audible, dict) and not str(marker.get("cover_url") or "").strip():
                marker["cover_url"] = cover_url
                changed = True
        sidecar = data.get("sidecar")
        if isinstance(sidecar, dict):
            book = sidecar.get("book")
            if isinstance(book, dict) and not str(book.get("cover_url") or "").strip():
                book["cover_url"] = cover_url
                changed = True
        if changed:
            lf_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            stamped = True
    if not stamped:
        # No sidecar yet — write a minimal companion next to staging root.
        target = staging / "metadata.json"
        payload = {"cover_url": cover_url}
        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload = {**existing, "cover_url": cover_url}
            except (OSError, json.JSONDecodeError):
                pass
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
