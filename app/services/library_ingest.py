"""Owned-audiobook + Library Sweep ingest helpers.

Creates synthetic DownloadRequests (no debrid), stages files into
``.unorganized/req_*``, and kicks ``run_forge_after_download`` from metadata.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from sqlalchemy import select

from app.database import async_session
from app.models import DownloadRequest
from app.services import downloader
from app.services.forge_pipeline import (
    AUDIO_EXTENSIONS,
    _collect_audio,
    audiobook_staging_dir,
    run_forge_after_download,
    staging_path_for_libraforge,
)

logger = logging.getLogger(__name__)

SOURCE_REQUEST = "request"
SOURCE_SWEEP = "sweep"
SOURCE_UPLOAD = "upload"

_ACTIVE_FORGE = frozenset({
    "pending",
    "metadata_forge",
    "m4b_convert",
    "chapter_forge",
    "folder_forge",
    "finalizing",
})

# Terminal outcomes that Library Sweep should not auto-reprocess on walk.
_UNPROCESSED_STATUSES = frozenset({
    "cancelled",
    "failed",
    "skipped",
    "admin_rejected",
})


def sweep_magnet(abs_item_id: str) -> str:
    return f"sweep:abs:{abs_item_id}"


def upload_magnet(token: str | None = None) -> str:
    return f"upload:{token or uuid.uuid4().hex}"


def sweep_fingerprint(abs_item_id: str) -> str:
    return f"abs:{abs_item_id}"


def is_synthetic_magnet(magnet: str | None) -> bool:
    """True for sweep:/upload: magnets that must never hit Real-Debrid."""
    m = (magnet or "").strip().lower()
    return m.startswith("sweep:") or m.startswith("upload:")


def is_local_ingest_source(source: str | None) -> bool:
    return (source or "").strip().lower() in (SOURCE_SWEEP, SOURCE_UPLOAD)


def is_local_ingest_request(req: DownloadRequest | Any) -> bool:
    """Sweep/upload rows — forge-only; never debrid ``process_download``."""
    return is_local_ingest_source(getattr(req, "source", None)) or is_synthetic_magnet(
        getattr(req, "magnet_link", None)
    )

def _link_or_copy(src: str, dst: str) -> None:
    """Prefer hardlink (same filesystem); fall back to copy2."""
    try:
        os.link(src, dst)
        return
    except OSError:
        pass
    shutil.copy2(src, dst)


def stage_tree_from_library(src: Path, dest: Path, *, prefer_hardlink: bool = True) -> str:
    """Copy or hardlink a library folder into staging. Returns method used."""
    if not src.is_dir():
        raise FileNotFoundError(f"Source folder missing: {src}")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    copy_fn = _link_or_copy if prefer_hardlink else shutil.copy2
    method = "hardlink" if prefer_hardlink else "copy"
    try:
        shutil.copytree(src, dest, copy_function=copy_fn)
    except OSError:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest, copy_function=shutil.copy2)
        method = "copy"
    if prefer_hardlink and method == "hardlink":
        # Verify at least one hardlink succeeded; else rewrite as copy.
        sample = next((p for p in dest.rglob("*") if p.is_file()), None)
        if sample is not None:
            try:
                if sample.stat().st_nlink < 2:
                    # Still a regular copy on some FS even after link attempt —
                    # leave as-is; files are present.
                    pass
            except OSError:
                pass
    return method


def stage_uploaded_files(
    dest: Path,
    files: Iterable[tuple[str, bytes | Any]],
) -> int:
    """Write uploaded ``(filename, fileobj_or_bytes)`` into ``dest``. Returns file count."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, payload in files:
        safe = downloader.sanitize_filename(Path(name).name) or f"file_{count}"
        target = dest / safe
        if target.exists():
            stem, suffix = target.stem, target.suffix
            target = dest / f"{stem}_{count}{suffix}"
        if isinstance(payload, (bytes, bytearray)):
            target.write_bytes(payload)
        else:
            # Starlette UploadFile / file-like
            data = payload.read() if hasattr(payload, "read") else bytes(payload)
            if hasattr(data, "__await__"):
                raise TypeError("stage_uploaded_files expects sync read; await UploadFile.read() first")
            target.write_bytes(data)
        count += 1
    return count


async def fingerprint_already_swept(fingerprint: str) -> bool:
    """True when a prior sweep ingest for this fingerprint completed successfully."""
    if not fingerprint:
        return False
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest.id).where(
                DownloadRequest.ingest_fingerprint == fingerprint,
                DownloadRequest.source == SOURCE_SWEEP,
                DownloadRequest.status == "completed",
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def fingerprint_in_flight(fingerprint: str) -> bool:
    if not fingerprint:
        return False
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest.id).where(
                DownloadRequest.ingest_fingerprint == fingerprint,
                DownloadRequest.source == SOURCE_SWEEP,
                DownloadRequest.status.in_(_ACTIVE_FORGE),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def latest_sweep_request(fingerprint: str) -> DownloadRequest | None:
    """Most recent sweep DownloadRequest for this ABS fingerprint (any status)."""
    if not fingerprint:
        return None
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest)
            .where(
                DownloadRequest.ingest_fingerprint == fingerprint,
                DownloadRequest.source == SOURCE_SWEEP,
            )
            .order_by(DownloadRequest.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def retry_quarantined_ingest(
    request_id: int,
    *,
    user_id: int,
    title: str,
    author: str | None,
    handoff_m4b: bool = False,
) -> dict[str, Any]:
    """Re-kick forge from metadata for an existing quarantined sweep request."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        staging_raw = (req.staging_path or "").strip()
        stored_title = req.title or title
        stored_author = req.author if req.author is not None else author

    if not staging_raw:
        return {
            "ok": False,
            "id": request_id,
            "status": "failed",
            "reason": "missing_staging",
        }

    staging = Path(staging_raw)
    if not staging.is_dir():
        return {
            "ok": False,
            "id": request_id,
            "status": "failed",
            "reason": "staging_missing",
        }

    from app.services.forge_pipeline import needs_m4b_conversion

    needs_m4b = needs_m4b_conversion(staging)
    result = await prepare_staging_and_forge(
        request_id,
        staging=staging,
        user_id=user_id,
        title=title or stored_title,
        author=author if author is not None else stored_author,
        kick_forge=True,
        handoff_m4b=handoff_m4b,
    )
    result["needs_m4b"] = needs_m4b
    result["retried"] = True
    return result


async def reprocess_local_ingest(
    request_id: int,
    *,
    handoff_m4b: bool = False,
) -> dict[str, Any]:
    """Manual reprocess for cancelled/failed/skipped/quarantined sweep or upload."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        if not is_local_ingest_request(req):
            raise ValueError("Not a sweep/upload ingest request")
        if req.status in _ACTIVE_FORGE:
            raise ValueError(f"Request already in progress ({req.status})")
        user_id = int(req.user_id)
        title = req.title or "Untitled"
        author = req.author
        staging_raw = (req.staging_path or "").strip()

    if not staging_raw:
        return {
            "ok": False,
            "id": request_id,
            "status": "failed",
            "reason": "missing_staging",
        }
    staging = Path(staging_raw)
    if not staging.is_dir():
        from app.services.forge_pipeline import resolve_staging_dir

        try:
            staging = resolve_staging_dir(staging_raw)
        except FileNotFoundError:
            return {
                "ok": False,
                "id": request_id,
                "status": "failed",
                "reason": "staging_missing",
            }

    from app.services.forge_pipeline import needs_m4b_conversion

    needs_m4b = needs_m4b_conversion(staging)
    result = await prepare_staging_and_forge(
        request_id,
        staging=staging,
        user_id=user_id,
        title=title,
        author=author,
        kick_forge=True,
        handoff_m4b=handoff_m4b,
    )
    result["needs_m4b"] = needs_m4b
    result["reprocessed"] = True
    return result


async def create_ingest_request(
    *,
    user_id: int,
    title: str,
    author: str | None,
    source: str,
    magnet_link: str,
    abs_item_id: str | None = None,
    ingest_fingerprint: str | None = None,
    cover_url: str | None = None,
    indexer: str | None = None,
) -> DownloadRequest:
    """Insert a DownloadRequest for sweep/upload. Does **not** call process_download."""
    async with async_session() as db:
        req = DownloadRequest(
            user_id=user_id,
            title=(title or "Untitled")[:512],
            author=(author or None) and (author[:256] if author else None),
            magnet_link=magnet_link,
            indexer=indexer or ("Library Sweep" if source == SOURCE_SWEEP else "Owned upload"),
            media_type="audiobook",
            status="pending",
            status_detail="Staged for forge (no download)",
            source=source,
            abs_item_id=abs_item_id,
            ingest_fingerprint=ingest_fingerprint,
            cover_url=(cover_url or None) and cover_url[:1024],
            is_private=False,
        )
        db.add(req)
        await db.commit()
        await db.refresh(req)
        return req


async def prepare_staging_and_forge(
    request_id: int,
    *,
    staging: Path,
    user_id: int,
    title: str,
    author: str | None = None,
    kick_forge: bool = True,
    handoff_m4b: bool = False,
) -> dict[str, Any]:
    """Persist staging path, set metadata_forge, optionally run forge from metadata.

    ``handoff_m4b``: for Library Sweep — after metadata, queue M4B in the background
    and return so the sweep worker can advance to the next book.
    """
    if not staging.is_dir() or not _collect_audio(staging):
        async with async_session() as db:
            result = await db.execute(
                select(DownloadRequest).where(DownloadRequest.id == request_id)
            )
            req = result.scalar_one_or_none()
            if req:
                req.status = "quarantined"
                req.quarantine_reason = "Ingest staging has no audio files"
                req.staging_path = staging_path_for_libraforge(staging)
                req.status_detail = req.quarantine_reason
                await db.commit()
        return {
            "ok": False,
            "id": request_id,
            "status": "quarantined",
            "reason": "no_audio",
        }

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if not req:
            raise FileNotFoundError(f"Request {request_id} not found")
        req.staging_path = staging_path_for_libraforge(staging)
        req.status = "metadata_forge"
        req.status_detail = "Matching metadata via LibraForge…"
        req.quarantine_reason = None
        await db.commit()

    if kick_forge:
        await run_forge_after_download(
            request_id,
            staging=staging,
            user_id=user_id,
            title=title,
            author=author,
            resume_from="metadata",
            handoff_m4b=handoff_m4b,
        )

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        status = req.status if req else "unknown"
        reason = req.quarantine_reason if req else None

    return {
        "ok": True,
        "id": request_id,
        "status": status,
        "quarantine_reason": reason,
        "staging_path": staging_path_for_libraforge(staging),
        "needs_m4b": False,  # caller may set before forge
        "m4b_handed_off": bool(handoff_m4b and status == "m4b_convert"),
    }


async def ingest_from_library_folder(
    *,
    user_id: int,
    title: str,
    author: str | None,
    library_dir: Path,
    source: str,
    magnet_link: str,
    abs_item_id: str | None = None,
    ingest_fingerprint: str | None = None,
    cover_url: str | None = None,
    kick_forge: bool = True,
    handoff_m4b: bool = False,
    on_request_created: Callable[[int], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    """Create request, stage library folder, run forge. Skips debrid entirely."""
    req = await create_ingest_request(
        user_id=user_id,
        title=title,
        author=author,
        source=source,
        magnet_link=magnet_link,
        abs_item_id=abs_item_id,
        ingest_fingerprint=ingest_fingerprint,
        cover_url=cover_url,
    )
    if on_request_created is not None:
        maybe = on_request_created(req.id)
        if maybe is not None and hasattr(maybe, "__await__"):
            await maybe
    staging = audiobook_staging_dir(req.id, title)
    method = stage_tree_from_library(library_dir, staging, prefer_hardlink=True)
    logger.info(
        "Ingest request %s staged from %s via %s → %s",
        req.id,
        library_dir,
        method,
        staging,
    )
    from app.services.forge_pipeline import needs_m4b_conversion

    needs_m4b = needs_m4b_conversion(staging)
    result = await prepare_staging_and_forge(
        req.id,
        staging=staging,
        user_id=user_id,
        title=title,
        author=author,
        kick_forge=kick_forge,
        handoff_m4b=handoff_m4b,
    )
    result["stage_method"] = method
    result["needs_m4b"] = needs_m4b
    return result


async def ingest_uploaded_audiobook(
    *,
    user_id: int,
    title: str,
    author: str | None,
    file_blobs: list[tuple[str, bytes]],
    kick_forge: bool = True,
) -> dict[str, Any]:
    """Create upload request, write files into staging, run forge."""
    token = uuid.uuid4().hex
    req = await create_ingest_request(
        user_id=user_id,
        title=title,
        author=author,
        source=SOURCE_UPLOAD,
        magnet_link=upload_magnet(token),
        ingest_fingerprint=f"upload:{token}",
        indexer="Owned upload",
    )
    staging = audiobook_staging_dir(req.id, title)
    count = stage_uploaded_files(staging, file_blobs)
    if count == 0:
        async with async_session() as db:
            result = await db.execute(
                select(DownloadRequest).where(DownloadRequest.id == req.id)
            )
            row = result.scalar_one_or_none()
            if row:
                row.status = "failed"
                row.status_detail = "No files in upload"
                await db.commit()
        return {"ok": False, "id": req.id, "status": "failed", "reason": "empty"}

    # Drop non-audio junk quietly? Keep all; forge / quarantine handles it.
    audio = _collect_audio(staging)
    if not audio:
        # Still allow if user uploaded archives later — but for now quarantine.
        pass

    from app.services.forge_pipeline import needs_m4b_conversion

    needs_m4b = bool(audio) and needs_m4b_conversion(staging)
    result = await prepare_staging_and_forge(
        req.id,
        staging=staging,
        user_id=user_id,
        title=title,
        author=author,
        kick_forge=kick_forge,
    )
    result["file_count"] = count
    result["needs_m4b"] = needs_m4b
    return result


def is_audio_filename(name: str) -> bool:
    return Path(name).suffix.lower() in AUDIO_EXTENSIONS


async def user_may_upload_owned(user_role: str) -> bool:
    """Admins always; users when allow_user_audiobook_upload is true."""
    if (user_role or "").lower() == "admin":
        return True
    from app.services import instance_settings

    return await instance_settings.get_effective_bool(
        "allow_user_audiobook_upload", default=False
    )
