"""OpenRouter LLM assist orchestration for forge / ebook / Quick Review.

Stores structured suggestions in staging ``llm_assist.json`` (never secrets).
All entry points soft-fail to existing pipeline behavior when assist is off
or the model is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from app.database import async_session
from app.models import DownloadRequest
from app.services import downloader, openrouter
from app.services.forge_pipeline import (
    _FORMAT_DIR_NAMES,
    _FORMAT_EXT_RANK,
    AUDIO_EXTENSIONS,
    _audio_parent_dirs,
    _collect_audio,
    _set_quarantine,
    audiobook_staging_dir,
    clean_catalog_title,
    collect_staging_llm_context,
    delete_staging_entry,
    extract_asin_from_staging,
    normalize_asin,
    resolve_staging_dir,
    safe_path_under_staging,
    seed_staging_metadata_hints,
    staging_path_for_libraforge,
)

logger = logging.getLogger(__name__)

ASSIST_FILENAME = "llm_assist.json"
_SAMPLE_NAME_RE = re.compile(
    r"(?:^|[\s._-])(?:sample|preview|excerpt|promo|teaser)(?:[\s._-]|$)",
    re.IGNORECASE,
)

HandleResult = Literal["continue", "quarantined", "split"]


def _pipeline():
    from app.services import pipeline as p
    return p


def assist_path(staging: Path) -> Path:
    return staging / ASSIST_FILENAME


def read_assist(staging: Path) -> dict[str, Any]:
    path = assist_path(staging)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_assist(staging: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into staging llm_assist.json and return the full doc."""
    staging.mkdir(parents=True, exist_ok=True)
    data = read_assist(staging)
    data.update(patch)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        assist_path(staging).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("Could not write %s: %s", assist_path(staging), e)
    return data


def detect_likely_multi_book(staging: Path) -> bool:
    """True when staging looks like multiple titles (not dual-format siblings)."""
    parents = _audio_parent_dirs(staging)
    if len(parents) <= 1:
        return False
    format_dirs = [p for p in parents if p.name.lower() in _FORMAT_DIR_NAMES]
    if format_dirs and len(format_dirs) == len(parents):
        return False
    book_dirs = [p for p in parents if p.name.lower() not in _FORMAT_DIR_NAMES]
    return len(book_dirs) >= 2


def detect_dual_format(staging: Path) -> bool:
    """Sibling format folders (mp3/ + AAC/) for the same title."""
    parents = _audio_parent_dirs(staging)
    if len(parents) < 2:
        return False
    format_dirs = [p for p in parents if p.name.lower() in _FORMAT_DIR_NAMES]
    return bool(format_dirs) and len(format_dirs) == len(parents)


def _paths_are_clear_folders(staging: Path, plan: openrouter.BookSplitPlan) -> bool:
    """Each book group maps to existing top-level folders (or dirs under staging)."""
    if not plan.folder_based:
        return False
    for book in plan.books:
        if not book.paths:
            return False
        for rel in book.paths:
            try:
                target = safe_path_under_staging(staging, rel)
            except ValueError:
                return False
            if not target.exists():
                return False
            # Prefer directories; allow a single file only if it's the whole group.
            if target.is_file() and len(book.paths) > 1:
                return False
    return True


def _split_paths_ready(staging: Path, plan: openrouter.BookSplitPlan) -> bool:
    """True when every book path exists under staging (folders or files)."""
    if _paths_are_clear_folders(staging, plan):
        return True
    for book in plan.books:
        if not book.paths:
            return False
        for rel in book.paths:
            try:
                target = safe_path_under_staging(staging, rel)
            except ValueError:
                return False
            if not target.exists():
                return False
    return True


async def _load_release_files_for_request(
    request_id: int,
    staging: Path,
) -> list[dict[str, Any]]:
    """Load ABB/debrid file list from DB, then staging assist sidecar."""
    from app.services.release_files import loads_release_files, normalize_release_files

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if req and getattr(req, "release_files_json", None):
            files = loads_release_files(req.release_files_json)
            if files:
                return files
    assist = read_assist(staging)
    return normalize_release_files(assist.get("release_files"))


async def maybe_handle_multi_book(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    title: str,
    author: str | None,
) -> HandleResult:
    """Detect multi-book packs; auto-split or quarantine with plan for Quick Review."""
    from app.services.release_files import (
        build_split_plan_from_release_files,
        group_release_files_by_book,
    )

    release_files = await _load_release_files_for_request(request_id, staging)
    if release_files:
        write_assist(staging, {"release_files": release_files[:500]})

    heuristic_raw = build_split_plan_from_release_files(
        staging,
        release_files,
        default_author=author or "",
    )
    release_groups = group_release_files_by_book(release_files)
    likely = detect_likely_multi_book(staging) or len(release_groups) >= 2
    if not likely:
        return "continue"

    plan: openrouter.BookSplitPlan | None = None
    if heuristic_raw:
        plan = openrouter.parse_split_plan(heuristic_raw)

    if plan is None and await openrouter.is_enabled():
        context = {
            "request_title": title,
            "request_author": author or "",
            "audio_parent_dirs": [
                str(p.relative_to(staging)) if p != staging else "."
                for p in _audio_parent_dirs(staging)
            ],
            "release_files": [
                {"path": f.get("path"), "size_bytes": f.get("size_bytes")}
                for f in release_files[:200]
            ],
            "release_groups": [
                {
                    "title": g.get("title"),
                    "key": g.get("key"),
                    "audio_names": (g.get("audio_names") or [])[:40],
                }
                for g in release_groups[:40]
            ],
            **collect_staging_llm_context(staging),
        }
        try:
            plan = await openrouter.propose_multi_book_split(context)
        except Exception as e:  # pragma: no cover
            logger.warning("Multi-book assist error for request %s: %s", request_id, e)
            plan = None

    if not plan:
        if len(release_groups) >= 2:
            reason = (
                f"Multi-book pack ({len(release_groups)} titles in release file list) "
                "but files could not be mapped automatically — review in Quick Review"
            )[:500]
            write_assist(
                staging,
                {
                    "multi_book_status": "needs_review",
                    "release_groups": [
                        {"title": g.get("title"), "key": g.get("key")}
                        for g in release_groups
                    ],
                },
            )
            await _set_quarantine(request_id, reason, staging)
            return "quarantined"
        return "continue"

    write_assist(
        staging,
        {
            "multi_book_split": plan.to_dict(),
            "multi_book_status": "proposed",
            "release_files": release_files[:500] if release_files else None,
        },
    )

    threshold = await openrouter.get_confidence_threshold()
    if plan.confidence >= threshold and _split_paths_ready(staging, plan):
        try:
            child_ids = await apply_multi_book_split(
                request_id,
                staging=staging,
                plan=plan,
                spawn_forge=True,
            )
            if child_ids:
                logger.info(
                    "Auto-split request %s into children %s",
                    request_id,
                    child_ids,
                )
                return "split"
        except Exception as e:
            logger.warning(
                "Auto-split failed for request %s — quarantining with plan: %s",
                request_id,
                e,
            )

    reason = (
        f"Multi-book pack suggested ({len(plan.books)} books, "
        f"confidence {plan.confidence:.2f})"
        + (f": {plan.rationale}" if plan.rationale else "")
        + " — Apply split in Quick Review"
    )[:500]
    write_assist(staging, {"multi_book_status": "needs_review"})
    await _set_quarantine(request_id, reason, staging)
    return "quarantined"


async def apply_multi_book_split(
    request_id: int,
    *,
    staging: Path | None = None,
    plan: openrouter.BookSplitPlan | None = None,
    spawn_forge: bool = True,
) -> list[int]:
    """Move groups into child staging dirs, create child requests, spawn forge.

    Parent request is marked completed with a split summary. Returns child ids.
    """
    p = _pipeline()
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        parent = result.scalar_one_or_none()
        if not parent:
            raise ValueError(f"Request {request_id} not found")
        staging_str = (parent.staging_path or "").strip()
        user_id = parent.user_id
        parent_title = parent.title
        parent_author = parent.author
        parent_magnet = parent.magnet_link
        parent_indexer = parent.indexer
        parent_media = parent.media_type or "audiobook"
        parent_cover = parent.cover_url
        parent_volume = parent.google_volume_id
        parent_private = bool(parent.is_private)

    if staging is None:
        if not staging_str:
            raise ValueError("Request has no staging_path")
        staging = resolve_staging_dir(staging_str)

    if plan is None:
        stored = read_assist(staging).get("multi_book_split") or {}
        plan = openrouter.parse_split_plan(stored)
        if not plan:
            raise ValueError("No multi-book split plan available")

    child_ids: list[int] = []
    for idx, book in enumerate(plan.books, start=1):
        raw_child_title = book.title or f"{parent_title} (book {idx})"
        child_title = clean_catalog_title(raw_child_title) or raw_child_title
        child_author = book.author or parent_author
        async with async_session() as db:
            child = DownloadRequest(
                user_id=user_id,
                title=child_title[:512],
                author=(child_author or "")[:256] or None,
                magnet_link=f"{parent_magnet}#llm_split_{request_id}_{idx}",
                indexer=parent_indexer,
                media_type=parent_media,
                is_private=parent_private,
                google_volume_id=parent_volume,
                cover_url=parent_cover,
                status="pending",
                status_detail=f"Split from request #{request_id}",
            )
            db.add(child)
            await db.flush()
            await db.refresh(child)
            child_id = child.id
            await db.commit()

        child_staging = audiobook_staging_dir(child_id, child_title)
        child_staging.mkdir(parents=True, exist_ok=True)

        for rel in book.paths:
            src = safe_path_under_staging(staging, rel)
            if not src.exists():
                logger.warning("Split path missing for child %s: %s", child_id, rel)
                continue
            dest = child_staging / src.name
            if dest.exists():
                dest = child_staging / f"{idx}_{src.name}"
            try:
                shutil.move(str(src), str(dest))
            except OSError as e:
                logger.warning("Could not move %s → %s: %s", src, dest, e)

        if not _collect_audio(child_staging):
            logger.warning("Child %s has no audio after split — skipping forge", child_id)
            async with async_session() as db:
                result = await db.execute(
                    select(DownloadRequest).where(DownloadRequest.id == child_id)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.status = "quarantined"
                    row.quarantine_reason = "Split produced empty staging"
                    row.staging_path = staging_path_for_libraforge(child_staging)
                    await db.commit()
            child_ids.append(child_id)
            continue

        seed_staging_metadata_hints(
            child_staging,
            title=child_title,
            author=child_author,
            force=True,
        )
        async with async_session() as db:
            result = await db.execute(
                select(DownloadRequest).where(DownloadRequest.id == child_id)
            )
            row = result.scalar_one_or_none()
            if row:
                row.staging_path = staging_path_for_libraforge(child_staging)
                row.status = "metadata_forge"
                row.status_detail = "Starting forge after multi-book split…"
                await db.commit()

        child_ids.append(child_id)
        if spawn_forge:
            from app.services.forge_pipeline import run_forge_after_download

            asyncio.create_task(
                run_forge_after_download(
                    child_id,
                    staging=child_staging,
                    user_id=user_id,
                    title=child_title,
                    author=child_author,
                )
            )

    # Mark parent done; leave leftover staging if anything remains.
    leftover = _collect_audio(staging)
    detail = (
        f"Split into {len(child_ids)} books: "
        + ", ".join(f"#{i}" for i in child_ids)
    )[:500]
    write_assist(
        staging,
        {
            "multi_book_status": "applied",
            "multi_book_child_ids": child_ids,
        },
    )
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        parent = result.scalar_one_or_none()
        if parent:
            parent.status = "completed" if not leftover else "quarantined"
            parent.status_detail = detail
            parent.quarantine_reason = (
                None if not leftover else "Leftover files after multi-book split"
            )
            parent.progress_percent = 100.0 if not leftover else None
            await db.commit()
            await p._report_progress(
                request_id,
                user_id,
                parent.status,
                detail,
                progress_percent=100.0 if not leftover else None,
            )

    if not leftover:
        try:
            # Keep assist sidecar? wipe whole tree — parent is done.
            shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            pass

    return child_ids


async def maybe_auto_prune_or_suggest(
    request_id: int,
    *,
    staging: Path,
    force_suggest: bool = False,
) -> dict[str, Any] | None:
    """Before metadata / on Quick Review Files: prune safe duplicates or store plan."""
    if not await openrouter.is_enabled():
        return None

    existing = read_assist(staging).get("file_prune")
    if existing and not force_suggest:
        return {"auto_deleted": [], "plan": existing, "cached": True}

    if not force_suggest and not detect_dual_format(staging) and not _has_sample_files(staging):
        # Still useful when many formats coexist loosely — skip if tiny staging.
        audio = _collect_audio(staging)
        if len(audio) < 2:
            return None

    context = {
        "request_id": request_id,
        "dual_format": detect_dual_format(staging),
        **collect_staging_llm_context(staging),
    }
    try:
        plan = await openrouter.propose_file_prune(context)
    except Exception as e:  # pragma: no cover
        logger.warning("File prune assist error for request %s: %s", request_id, e)
        return None

    if not plan:
        return None

    write_assist(staging, {"file_prune": plan.to_dict(), "file_prune_status": "proposed"})

    threshold = await openrouter.get_confidence_threshold()
    if plan.confidence >= threshold:
        deleted = apply_file_prune(
            staging,
            plan,
            only_safe_duplicates=True,
        )
        if deleted:
            write_assist(
                staging,
                {
                    "file_prune_status": "auto_applied",
                    "file_prune_deleted": deleted,
                },
            )
            logger.info(
                "Auto-pruned %d safe duplicate(s) for request %s",
                len(deleted),
                request_id,
            )
            return {"auto_deleted": deleted, "plan": plan.to_dict()}

    write_assist(staging, {"file_prune_status": "needs_review"})
    return {"auto_deleted": [], "plan": plan.to_dict()}


def _has_sample_files(staging: Path) -> bool:
    for audio in _collect_audio(staging):
        if _SAMPLE_NAME_RE.search(audio.name):
            return True
    return False


def apply_file_prune(
    staging: Path,
    plan: openrouter.FilePrunePlan | dict[str, Any] | None = None,
    *,
    paths: list[str] | None = None,
    only_safe_duplicates: bool = False,
) -> list[str]:
    """Delete staging paths from a prune plan. Path-safe; never remove last audio."""
    if plan is None and paths is None:
        stored = read_assist(staging).get("file_prune")
        plan = openrouter.parse_prune_plan(stored)

    to_delete: list[str] = []
    if paths is not None:
        to_delete = [p.replace("\\", "/").lstrip("/") for p in paths if p]
    elif isinstance(plan, openrouter.FilePrunePlan):
        for action in plan.actions:
            if action.action != "delete":
                continue
            if only_safe_duplicates and not action.safe_duplicate:
                continue
            to_delete.append(action.path)
    elif isinstance(plan, dict):
        parsed = openrouter.parse_prune_plan(plan)
        if parsed:
            return apply_file_prune(
                staging, parsed, only_safe_duplicates=only_safe_duplicates
            )

    if not to_delete:
        return []

    audio_before = {str(a.relative_to(staging)).replace("\\", "/") for a in _collect_audio(staging)}
    deleted: list[str] = []
    for rel in to_delete:
        rel_n = rel.replace("\\", "/").lstrip("/")
        if ".." in rel_n.split("/"):
            continue
        try:
            target = safe_path_under_staging(staging, rel_n)
        except ValueError:
            continue
        if not target.exists() or target == staging.resolve():
            continue

        # Never delete the sole remaining audio file.
        if target.is_file() and target.suffix.lower() in AUDIO_EXTENSIONS:
            remaining = audio_before - set(deleted) - {rel_n}
            if not remaining:
                logger.info("Skipping prune of sole audio: %s", rel_n)
                continue
        elif target.is_dir():
            dir_audio = [
                str(a.relative_to(staging)).replace("\\", "/")
                for a in _collect_audio(target)
            ]
            remaining = audio_before - set(deleted) - set(dir_audio)
            if dir_audio and not remaining:
                logger.info("Skipping prune of sole audio folder: %s", rel_n)
                continue

        try:
            delete_staging_entry(staging, rel_n)
            deleted.append(rel_n)
            for a in list(audio_before):
                if a == rel_n or a.startswith(rel_n.rstrip("/") + "/"):
                    audio_before.discard(a)
        except (OSError, ValueError, FileNotFoundError) as e:
            logger.warning("Prune delete failed for %s: %s", rel_n, e)

    if deleted:
        write_assist(
            staging,
            {
                "file_prune_status": "applied",
                "file_prune_deleted": deleted,
            },
        )
    return deleted


async def verify_asin_with_libraforge(asin: str, staging: Path) -> bool:
    """True when LibraForge Audible chaptering accepts the ASIN (preview / no_save)."""
    from app.services import libraforge
    from app.services.forge_pipeline import primary_audio_for_chaptering

    asin_n = normalize_asin(asin)
    if not asin_n:
        return False

    audio = primary_audio_for_chaptering(staging)
    # Prefer any audio path for chaptering_load; fall back to staging root.
    if audio is not None:
        source_path = staging_path_for_libraforge(audio)
    else:
        audio_list = _collect_audio(staging)
        if not audio_list:
            return False
        source_path = staging_path_for_libraforge(audio_list[0])

    try:
        run_id = await libraforge.start_chaptering_run(
            source_path,
            asin=asin_n,
            backend="audible-chapters",
            no_save=True,
        )
        report = await libraforge.wait_for_run(
            run_id,
            poll_seconds=2.0,
            timeout_seconds=120.0,
        )
    except Exception as e:
        logger.info("ASIN verify failed for %s: %s", asin_n, e)
        return False

    if libraforge.run_failed(report):
        return False

    stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
    chapters = 0
    try:
        chapters = int(stats.get("chapters") or 0)
    except (TypeError, ValueError):
        chapters = 0
    if chapters <= 0:
        # Some reports nest chapters in the body.
        nested = report.get("chapters") or report.get("preview_chapters") or []
        if isinstance(nested, list):
            chapters = len(nested)
    return chapters > 0


async def maybe_recover_asin(
    request_id: int,
    *,
    staging: Path,
    title: str,
    author: str | None,
) -> str | None:
    """After metadata when ASIN missing: suggest + verify; stamp or store for review.

    Returns stamped ASIN, or None (continue without chapters / quarantine suggestion).
    """
    if extract_asin_from_staging(staging):
        return None
    if not await openrouter.is_enabled():
        return None

    title_s = (title or "").strip()
    author_s = (author or "").strip()
    if not title_s:
        # Try sidecar tags
        ctx_tags = collect_staging_llm_context(staging).get("partial_tags") or {}
        title_s = str(ctx_tags.get("title") or "").strip()
        author_s = author_s or str(ctx_tags.get("author") or "").strip()
    if not title_s:
        return None

    context = {
        "title": title_s,
        "author": author_s,
        "request_id": request_id,
        **collect_staging_llm_context(staging),
    }
    try:
        suggestion = await openrouter.suggest_asin(context)
    except Exception as e:  # pragma: no cover
        logger.warning("ASIN assist error for request %s: %s", request_id, e)
        return None

    if not suggestion or not suggestion.asin:
        return None

    write_assist(staging, {"asin_recovery": suggestion.to_dict(), "asin_status": "proposed"})

    threshold = await openrouter.get_confidence_threshold()
    verified = await verify_asin_with_libraforge(suggestion.asin, staging)
    write_assist(
        staging,
        {
            "asin_recovery": {**suggestion.to_dict(), "verified": verified},
            "asin_status": "verified" if verified else "unverified",
        },
    )

    if suggestion.confidence >= threshold and verified:
        seed_staging_metadata_hints(
            staging,
            title=suggestion.title or title_s,
            author=suggestion.author or author_s,
            asin=suggestion.asin,
            force=True,
        )
        write_assist(staging, {"asin_status": "applied"})
        logger.info(
            "ASIN recovered for request %s: %s (confidence=%.2f, verified)",
            request_id,
            suggestion.asin,
            suggestion.confidence,
        )
        return suggestion.asin

    # Soft continue without chapters — store suggestion for Quick Review.
    logger.info(
        "ASIN suggestion for request %s not auto-applied "
        "(confidence=%.2f threshold=%.2f verified=%s)",
        request_id,
        suggestion.confidence,
        threshold,
        verified,
    )
    return None


async def ebook_identify_assist(
    *,
    staging: Path,
    title_hint: str,
    author_hint: str,
    prior_reason: str,
) -> openrouter.BookIdentification | None:
    """Mirror audiobook metadata assist for the ebook identify path."""
    if not await openrouter.is_enabled():
        return None

    from app.services.ebook_pipeline import pick_primary_ebook

    files: list[dict[str, Any]] = []
    if staging.is_dir():
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append({"path": str(path.relative_to(staging)), "size": size})
            if len(files) >= 40:
                break

    primary = pick_primary_ebook(staging)
    context = {
        "media_type": "ebook",
        "request_title": title_hint,
        "request_author": author_hint,
        "prior_failure": prior_reason[:500],
        "primary_file": primary.name if primary else "",
        "files": files,
    }
    try:
        hit = await openrouter.identify_book(context)
    except Exception as e:  # pragma: no cover
        logger.warning("Ebook LLM identify error: %s", e)
        return None

    if not hit:
        return None

    write_assist(
        staging,
        {
            "ebook_identify": {
                "title": hit.title,
                "author": hit.author,
                "series": hit.series,
                "asin": hit.asin,
                "confidence": hit.confidence,
                "rationale": hit.rationale,
            }
        },
    )
    return hit


def format_rank_for_path(path: Path) -> int:
    return _FORMAT_EXT_RANK.get(path.suffix.lower(), 0)
