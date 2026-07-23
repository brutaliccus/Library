"""Post-download LibraForge orchestration: metadata → M4B → Folder Forge → ABS."""

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

UNORGANIZED_DIRNAME = "_unorganized"
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


def audiobook_staging_dir(request_id: int, title: str) -> Path:
    """Per-request landing folder under `_unorganized` (not final library layout)."""
    slug = downloader.sanitize_filename(title or "book")[:80] or "book"
    return unorganized_root() / f"req_{request_id}_{slug}"


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
    async with async_session() as db:
        review_url = libraforge.public_manual_review_url()
        detail = reason[:500]
        if review_url:
            detail = f"{reason[:350]} · Review: {review_url}"
        await p._update_status(db, request_id, "quarantined", detail)
        result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
        req = result.scalar_one_or_none()
        if req:
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
    """Run Metadata Forge → M4B → Folder Forge → ABS scan.

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

        async with async_session() as db:
            await p._update_status(db, request_id, "metadata_forge", "Matching metadata via LibraForge…")

        async def _on_meta(state: dict[str, Any]) -> None:
            await _forge_progress(request_id, user_id, "metadata_forge", state)

        try:
            # Score ≥ min_score means match identity is trusted — not that the
            # torrent's existing tags/cover are correct. Force full overwrite.
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
            )
        except libraforge.LibraForgeError as e:
            await _set_quarantine(
                request_id,
                f"LibraForge Metadata Forge unavailable or failed: {e}",
                staging,
            )
            return

        if libraforge.run_failed(report):
            err = report.get("error") or report.get("status") or "Metadata Forge failed"
            await _set_quarantine(request_id, str(err)[:500], staging)
            return

        if not libraforge.metadata_auto_applied(report):
            await _set_quarantine(
                request_id,
                libraforge.quarantine_reason_from_report(report),
                staging,
            )
            return

        # Defense in depth: report said written — confirm markers on disk before
        # M4B / Folder Forge run on stale tags.
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
            return

        start_step = "m4b"

    if await p._is_cancelled(request_id):
        return

    # --- M4B (Pi LibraForge) ---
    if start_step == "m4b":
        if needs_m4b_conversion(staging):
            async with async_session() as db:
                await p._update_status(db, request_id, "m4b_convert", "Converting to M4B on Pi…")

            async def _on_m4b(state: dict[str, Any]) -> None:
                await _forge_progress(request_id, user_id, "m4b_convert", state)

            try:
                loaded = await libraforge.m4b_load(lf_path)
                meta = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}
                output_path = (
                    loaded.get("output_path")
                    or str((staging / f"{downloader.sanitize_filename(title) or staging.name}.m4b").as_posix())
                )
                # Prefer converting the staging folder as a whole
                input_path = lf_path
                run_id = await libraforge.start_m4b_run(
                    input_path,
                    str(output_path),
                    metadata=meta or {"title": title},
                    jobs=settings.libraforge_m4b_jobs,
                )
                await _persist_staging(request_id, staging, run_id=run_id)
                report = await libraforge.wait_for_run(
                    run_id,
                    poll_seconds=5.0,
                    timeout_seconds=settings.libraforge_m4b_timeout,
                    on_progress=_on_m4b,
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
            except libraforge.LibraForgeError as e:
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
            )
            if libraforge.run_failed(report):
                raise libraforge.LibraForgeError(
                    str(report.get("error") or report.get("status") or "Folder Forge failed")
                )
        except libraforge.LibraForgeError as e:
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

    if delete_files and staging_str:
        staging = Path(staging_str)
        # Also try under configured audiobook dir if path was relative-ish
        candidates = [staging]
        if not staging.is_absolute():
            candidates.append(Path(settings.audiobook_dir) / staging_str)
        for path in candidates:
            if path.is_dir() and UNORGANIZED_DIRNAME in path.parts:
                try:
                    shutil.rmtree(path, ignore_errors=True)
                    logger.info("Deleted quarantine staging %s", path)
                except OSError as e:
                    logger.warning("Could not delete staging %s: %s", path, e)

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
