import asyncio
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models import DownloadRequest, User
from app.services import real_debrid, audiobookshelf, kavita, downloader, annas_archive, goodreads, push
from app.utils.websocket import ws_manager

logger = logging.getLogger(__name__)
settings = get_settings()

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav", ".wma", ".aac", ".mp4"}

_progress_db_throttle: dict[int, float] = {}


def _format_speed(speed_bps: float) -> str:
    if speed_bps <= 0:
        return ""
    mbps = speed_bps / (1024 * 1024)
    if mbps >= 1:
        return f"{mbps:.1f} MB/s"
    return f"{speed_bps / 1024:.0f} KB/s"


def _rd_progress_detail(info: dict) -> tuple[str, float | None, float | None]:
    status = info.get("status", "")
    progress = float(info.get("progress") or 0)
    speed = float(info.get("speed") or 0)
    speed_str = _format_speed(speed)
    if status == "downloading":
        detail = f"Real-Debrid downloading… {progress:.0f}%"
        if speed_str:
            detail += f" · {speed_str}"
        return detail, progress, speed
    if status == "queued":
        return "Queued on Real-Debrid…", 0.0, None
    if status == "magnet_conversion":
        return "Converting magnet link…", 0.0, None
    if status == "waiting_files_selection":
        return "Selecting files on Real-Debrid…", 0.0, None
    return f"Real-Debrid: {status}", progress if progress > 0 else None, speed if speed > 0 else None


async def _report_progress(
    request_id: int,
    user_id: int,
    status: str,
    detail: str,
    *,
    progress_percent: float | None = None,
    progress_bytes: int | None = None,
    progress_total_bytes: int | None = None,
    progress_speed_bps: float | None = None,
    persist: bool = True,
):
    # Cancel / admin reject are terminal. In-flight RD/AA/forge progress must not revive them.
    if not persist:
        if await _is_cancelled(request_id):
            return
        await ws_manager.send_to_user(
            user_id,
            {
                "type": "status_update",
                "request_id": request_id,
                "status": status,
                "detail": detail,
                "progress_percent": progress_percent,
                "progress_bytes": progress_bytes,
                "progress_total_bytes": progress_total_bytes,
                "progress_speed_bps": progress_speed_bps,
            },
        )
        return

    now = time.monotonic()
    last = _progress_db_throttle.get(request_id, 0.0)
    throttled = now - last < 2.0 and progress_percent is not None and progress_percent < 100

    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.id == request_id)
        )
        req = result.scalar_one_or_none()
        if not req or req.status in ("cancelled", "admin_rejected"):
            return
        if not throttled:
            _progress_db_throttle[request_id] = now
            req.status = status
            req.status_detail = detail
            req.progress_percent = progress_percent
            req.progress_bytes = progress_bytes
            req.progress_total_bytes = progress_total_bytes
            req.progress_speed_bps = progress_speed_bps
            await db.commit()

    await ws_manager.send_to_user(
        user_id,
        {
            "type": "status_update",
            "request_id": request_id,
            "status": status,
            "detail": detail,
            "progress_percent": progress_percent,
            "progress_bytes": progress_bytes,
            "progress_total_bytes": progress_total_bytes,
            "progress_speed_bps": progress_speed_bps,
        },
    )


async def _is_cancelled(request_id: int) -> bool:
    """True when the request was stopped by the user or admin (abort in-flight work)."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest.status).where(DownloadRequest.id == request_id)
        )
        status = result.scalar_one_or_none()
        return status in ("cancelled", "admin_rejected")


async def _update_status(
    db: AsyncSession,
    request_id: int,
    status: str,
    detail: str | None = None,
    rd_torrent_id: str | None = None,
    progress_percent: float | None = None,
    progress_bytes: int | None = None,
    progress_total_bytes: int | None = None,
    progress_speed_bps: float | None = None,
):
    # Fresh lookup — long-lived download sessions + concurrent progress writers
    # can leave the identity map stale; never crash the pipeline on a missing row.
    result = await db.execute(select(DownloadRequest).where(DownloadRequest.id == request_id))
    req = result.scalar_one_or_none()
    if req is None:
        logger.warning("DownloadRequest %s missing during status update → %s", request_id, status)
        return
    # Never clobber an explicit user cancel / admin reject with pipeline progress/failure.
    if req.status == "cancelled" and status != "cancelled":
        return
    if req.status == "admin_rejected" and status != "admin_rejected":
        return
    req.status = status
    if detail is not None:
        req.status_detail = detail or status
    if rd_torrent_id is not None:
        req.rd_torrent_id = rd_torrent_id
    if status in ("completed", "failed", "cancelled", "quarantined", "admin_rejected"):
        req.progress_percent = None
        req.progress_bytes = None
        req.progress_total_bytes = None
        req.progress_speed_bps = None
        _progress_db_throttle.pop(request_id, None)
    else:
        if progress_percent is not None:
            req.progress_percent = progress_percent
        if progress_bytes is not None:
            req.progress_bytes = progress_bytes
        if progress_total_bytes is not None:
            req.progress_total_bytes = progress_total_bytes
        if progress_speed_bps is not None:
            req.progress_speed_bps = progress_speed_bps
    if status == "completed":
        req.completed_at = datetime.now(timezone.utc)
    await db.commit()
    try:
        await db.refresh(req)
    except Exception:
        pass

    await ws_manager.send_to_user(req.user_id, {
        "type": "status_update",
        "request_id": request_id,
        "status": status,
        "detail": detail,
        "progress_percent": req.progress_percent,
        "progress_bytes": req.progress_bytes,
        "progress_total_bytes": req.progress_total_bytes,
        "progress_speed_bps": req.progress_speed_bps,
    })


def _is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS


def _remove_tmpfiles(root: Path) -> None:
    """Remove -tmpfiles dirs left by auto-m4b or failed conversions."""
    for d in list(root.rglob("*-tmpfiles")):
        if d.is_dir():
            try:
                shutil.rmtree(d)
                logger.info(f"Removed leftover tmpfiles: {d}")
            except OSError as e:
                logger.warning(f"Could not remove tmpfiles {d}: {e}")


def _sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip(". ") or "Unknown"


def _is_collection_title(title: str) -> bool:
    """Detect titles like 'Books 1-6', 'Book 1-6', 'Vol 1-6' that indicate a multi-book collection."""
    lower = title.lower()
    return bool(re.search(r"\b(?:book|vol(?:ume)?|series)?\s*1\s*[-–]\s*[0-9]+\b", lower))


# Chapter / track folder names from common audiobook torrent layouts (incl. Audible rip folder-per-track).
_CHAPTER_FOLDER_RE = re.compile(
    r"^(?:chapter|ch|part|disc|track|cd)\s*\d+"
    r"|^(?:chapter|part|disc)\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
    r"|^\d{1,4}$"
    r"|^\d{1,4}\s*[-–]\s*.+"
    r"|^\d{2,}-\s*chapter\s*[-.]?\s*\d+"
    r"|^\d{2,}-\s*(audible|opening|closing|credits|introduction)\b"
    r"|[-–]\s*\d{1,4}\s*[-–]\s*chapter\s*\d+"  # "...Book 8 - 001 - Chapter 1"
    r"|\bchapter\s*\d+\b"  # "… Chapter 53" anywhere (IGNORECASE on whole pattern)
    r"|\bpart\s+\d{1,3}\s*$"  # "Book Title - Part 3"
    r"|\btrack\s*\d+\b",
    re.IGNORECASE,
)


def _looks_like_chapter_folder(name: str) -> bool:
    """Detect folder names like 'Chapter 01', '01', 'Disc 1', '02-chapter-1', '… - 001 - Chapter 1'."""
    return bool(_CHAPTER_FOLDER_RE.search(name.strip()))


def _chapter_subdirs_eligible_for_flatten(audio_sub_dirs: list[Path]) -> bool:
    """Allow Artwork/Lyrics subfolders; allow one extra nesting level if each folder has few audio files."""
    for d in audio_sub_dirs:
        files = [f for f in d.rglob("*") if f.is_file() and _is_audio(f)]
        if not files:
            return False
        if len(files) > 40:
            return False
    return True


def _flatten_chapter_subdirs(parent: Path) -> None:
    """If parent has only subdirs that look like chapter folders (each with 1-N audio files),
    flatten all audio into parent. Ensures Author/Book/audio instead of Author/Book/30 chapter dirs."""
    for d in list(parent.iterdir()):
        if d.is_dir():
            _flatten_chapter_subdirs(d)

    sub_dirs = [d for d in parent.iterdir() if d.is_dir()]
    loose_audio = [f for f in parent.iterdir() if _is_audio(f)]
    audio_sub_dirs = [d for d in sub_dirs if any(_is_audio(f) for f in d.rglob("*"))]
    if len(audio_sub_dirs) < 2:
        return

    if not _chapter_subdirs_eligible_for_flatten(audio_sub_dirs):
        logger.info(
            "Skip chapter flatten (per-dir audio count or empty): %s",
            parent,
        )
        return

    chapter_like = all(_looks_like_chapter_folder(d.name) for d in audio_sub_dirs)
    n_chapterish = sum(1 for d in audio_sub_dirs if _looks_like_chapter_folder(d.name))
    chapter_majority = n_chapterish >= max(2, (len(audio_sub_dirs) + 1) // 2)
    big_batch = len(audio_sub_dirs) >= 6
    should_flatten = chapter_like or big_batch or (len(audio_sub_dirs) >= 3 and chapter_majority)

    # A stray sample/trailer at book root must not block flattening a chapter-per-folder tree.
    if loose_audio and not should_flatten:
        return

    if should_flatten:
        for d in audio_sub_dirs:
            audio_files = [f for f in d.rglob("*") if f.is_file() and _is_audio(f)]
            for item in audio_files:
                target = parent / item.name
                if target != item and not target.exists():
                    shutil.move(str(item), str(target))
                elif target != item:
                    target = parent / f"{d.name} - {item.name}"
                    shutil.move(str(item), str(target))
            try:
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
        logger.info(f"Flattened {len(audio_sub_dirs)} chapter subdirs into {parent.name}/")


def _flatten_mixed_folder(parent: Path) -> None:
    """Flatten collection structure to Author/Book/audio.

    - Mixed (loose files + subdirs): loose files are whole-book files, move each
      into its own folder. Subdirs with many chapter mp3s stay as book folders.
    - Only loose files (2-5): treat as multiple whole-book files, one folder each.
    - Only loose files (6+): single book with chapters, leave as-is.
    - Recurse into batch folders (01-04, 05-06) to fully flatten.
    """
    sub_dirs = [d for d in parent.iterdir() if d.is_dir()]
    loose_audio = [f for f in parent.iterdir() if _is_audio(f)]
    audio_sub_dirs = [d for d in sub_dirs if any(_is_audio(f) for f in d.rglob("*"))]

    should_flatten_loose = (
        (loose_audio and audio_sub_dirs)
        or (len(loose_audio) >= 2 and len(loose_audio) <= 5 and not audio_sub_dirs)
    )
    if should_flatten_loose:
        for f in loose_audio:
            folder_name = _sanitize(f.stem)
            if not folder_name:
                continue
            new_dir = parent / folder_name
            new_dir.mkdir(exist_ok=True)
            shutil.move(str(f), str(new_dir / f.name))
            logger.info(f"Flattened whole-book file into {folder_name}/")

    for bd in audio_sub_dirs:
        nested = [d for d in bd.iterdir() if d.is_dir() and any(_is_audio(f) for f in d.rglob("*"))]
        nested_audio = [f for f in bd.iterdir() if _is_audio(f)]
        if len(nested) > 1 or (nested_audio and nested):
            _flatten_mixed_folder(bd)


def _collect_book_dirs(dest_dir: Path) -> list[Path]:
    """Recursively find folders that represent individual books.
    Handles nested structures like RAR/Batch1/Book1/, Batch1/Book2/, and
    mixed folders (loose files + subdirs) after flattening.
    Flattens chapter subdirs (e.g. Chapter 01/, Chapter 02/) into single book folder.
    """
    _flatten_mixed_folder(dest_dir)
    _flatten_chapter_subdirs(dest_dir)

    sub_dirs = [d for d in sorted(dest_dir.iterdir()) if d.is_dir()]
    audio_sub_dirs = [d for d in sub_dirs if any(_is_audio(f) for f in d.rglob("*"))]
    top_audio_files = [f for f in dest_dir.iterdir() if _is_audio(f)]

    if not audio_sub_dirs and not top_audio_files:
        return []

    if not top_audio_files:
        n_sub = len(audio_sub_dirs)
        if n_sub >= 3 and _chapter_subdirs_eligible_for_flatten(audio_sub_dirs):
            n_ch = sum(1 for d in audio_sub_dirs if _looks_like_chapter_folder(d.name))
            chapter_majority = n_ch >= max(2, (n_sub + 1) // 2)
            if n_sub >= 6 or all(_looks_like_chapter_folder(d.name) for d in audio_sub_dirs) or chapter_majority:
                # One book with per-chapter subdirs (flatten skipped e.g. stray root audio); never treat as N separate books.
                return [dest_dir]
        books: list[Path] = []
        for bd in audio_sub_dirs:
            nested = [d for d in bd.iterdir() if d.is_dir() and any(_is_audio(f) for f in d.rglob("*"))]
            nested_audio = [f for f in bd.iterdir() if _is_audio(f)]
            if len(nested) > 1 and not nested_audio:
                books.extend(_collect_book_dirs(bd))
            else:
                books.append(bd)
        return books

    # After chapter flatten, all tracks may sit loose in dest_dir — still one book folder.
    if top_audio_files and not audio_sub_dirs:
        return [dest_dir]
    if top_audio_files and audio_sub_dirs:
        return [dest_dir]

    return []


def _extract_series_prefix(name: str) -> str | None:
    """Extract series name from folder like 'White Trash Zombie 01 - Title' -> 'White Trash Zombie'."""
    m = re.match(r"^(.+?)\s+0?\d{1,2}\s*[-–]\s*.+", name.strip())
    return m.group(1).strip() if m else None


def _group_into_series(author_dir: Path, series_override: str | None = None) -> None:
    """Move books that share a series prefix into Author/Series/Book structure for ABS.
    If series_override is provided (e.g. from Goodreads), use it for the folder name.
    """
    subdirs = [d for d in author_dir.iterdir() if d.is_dir()]
    if len(subdirs) < 2:
        return

    by_series: dict[str, list[Path]] = {}
    for d in subdirs:
        prefix = _extract_series_prefix(d.name)
        if prefix:
            by_series.setdefault(prefix, []).append(d)

    for i, (regex_name, books) in enumerate(by_series.items()):
        if len(books) < 2:
            continue
        series_name = series_override if (series_override and i == 0) else regex_name
        series_dir = author_dir / _sanitize(series_name)
        series_dir.mkdir(parents=True, exist_ok=True)
        for bd in books:
            target = series_dir / bd.name
            if target.resolve() != bd.resolve():
                shutil.move(str(bd), str(target))
                logger.info(f"Grouped into series: {series_name}/{bd.name}")


def _split_collection(dest_dir: Path, author: str, series_override: str | None = None) -> list[Path]:
    """Lightweight collection splitter.

    Handles: (1) multiple subdirs each with audio, (2) nested Batch/Book1, Book2,
    (3) multiple single-file audiobooks at top level. Moves each into
    Author/BookTitle/ under the library root.
    """
    base_dir = dest_dir.parent

    book_dirs_to_move = _collect_book_dirs(dest_dir)
    top_audio = sorted(f for f in dest_dir.iterdir() if _is_audio(f))
    if len(book_dirs_to_move) > 1:
        logger.info(f"Collection detected: {len(book_dirs_to_move)} book subdirs in {dest_dir.name}")
        results = []
        for bd in book_dirs_to_move:
            final = base_dir / _sanitize(bd.name)
            if final.resolve() != bd.resolve():
                final.mkdir(parents=True, exist_ok=True)
                for item in bd.rglob("*"):
                    if item.is_file():
                        target = final / item.name
                        if target != item:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(item), str(target))
                shutil.rmtree(bd, ignore_errors=True)
            results.append(final)
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        _group_into_series(base_dir, series_override)
        return results

    if len(top_audio) > 1:
        audio_subdirs_with_files = [
            d for d in dest_dir.iterdir()
            if d.is_dir() and any(_is_audio(f) for f in d.rglob("*"))
        ]
        if len(top_audio) >= 6 and not audio_subdirs_with_files:
            return [dest_dir]
        keys: dict[str, list[Path]] = {}
        for f in top_audio:
            stem = re.sub(r"\(.*?\)|\[.*?\]", "", f.stem).strip()
            m = re.match(r"^(.+?)\s*[-_]\s*\d{1,2}\s*[-_]\s*(.+)$", stem)
            key = f"{m.group(1).strip()} - {m.group(2).strip()}" if m else stem
            keys.setdefault(key, []).append(f)

        if len(keys) > 1:
            logger.info(f"Collection detected: {len(keys)} distinct single-file books in {dest_dir.name}")
            results = []
            for group_name, files in keys.items():
                final = base_dir / _sanitize(group_name)
                final.mkdir(parents=True, exist_ok=True)
                for f in files:
                    shutil.move(str(f), str(final / f.name))
                results.append(final)
            shutil.rmtree(dest_dir, ignore_errors=True)
            _group_into_series(base_dir, series_override)
            return results

    return [dest_dir]


def audiobook_destination_dir(request_id: int, author: str, book_title: str) -> Path:
    """Legacy final-library path (Author/Title). Prefer staging when LibraForge pipeline is on."""
    if settings.libraforge_pipeline_enabled:
        from app.services.forge_pipeline import audiobook_staging_dir
        return audiobook_staging_dir(request_id, book_title or author)
    base = Path(settings.audiobook_dir)
    author_dir = base / downloader.sanitize_filename(author)
    if _is_collection_title(book_title):
        return author_dir / f"_incoming_{request_id}"
    return author_dir / downloader.sanitize_filename(book_title)


def organize_audiobook_files(
    dest_dir: Path,
    author: str,
    *,
    series_override: str | None = None,
) -> list[Path]:
    """Flatten per-chapter folders, split multi-book drops, remove tmpfiles. Safe to re-run."""
    dest_dir = Path(dest_dir).resolve()
    if not dest_dir.is_dir():
        raise FileNotFoundError(str(dest_dir))
    _remove_tmpfiles(dest_dir)
    book_dirs = _split_collection(dest_dir, author, series_override)
    for bd in book_dirs:
        _remove_tmpfiles(bd)
        _write_abs_metadata(bd, author=author, series=series_override)
    logger.info("Audiobook organize: %s book dir(s) from %s", len(book_dirs), dest_dir.name)
    return book_dirs


# Trailing release junk to strip from a folder name to get a display title
_TITLE_JUNK_RE = re.compile(
    r"[\[\(][^\]\)]*[\]\)]"
    r"|\b(?:m4b|mp3|flac|aac|opus)\b"
    r"|\b\d+\s*(?:k|kbps)\b"
    r"|\bunabridged\b|\babridged\b|\baudiobook\b",
    re.IGNORECASE,
)


def _title_from_folder(name: str) -> str:
    cleaned = _TITLE_JUNK_RE.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_.")
    return cleaned or name


def _write_abs_metadata(book_dir: Path, *, author: str, series: str | None = None) -> None:
    """Drop an Audiobookshelf metadata.json beside the audio files.

    Torrent audio tags are unreliable (or missing entirely), which is why the
    same book can show up split/mislabeled in ABS. ABS reads metadata.json in
    the book folder during scans and prefers it over bad embedded tags, so this
    pins the correct title/author/series deterministically. Never overwrites an
    existing file (ABS updates it once the user edits metadata in the UI)."""
    import json

    try:
        meta_path = book_dir / "metadata.json"
        if meta_path.exists():
            return
        if not any(_is_audio(f) for f in book_dir.rglob("*")):
            return

        title = _title_from_folder(book_dir.name)
        # "Series 03 - Title" folders: prefer the title part after the volume number
        m = re.match(r"^(.+?)\s+0?(\d{1,2})\s*[-–]\s*(.+)$", title)
        series_entry = None
        if m:
            series_name = series or m.group(1).strip()
            series_entry = f"{series_name} #{int(m.group(2))}"
            title = m.group(3).strip()
        elif series:
            series_entry = series

        meta = {
            "title": title,
            "authors": [author] if author and author.lower() != "unknown" else [],
        }
        if series_entry:
            meta["series"] = [series_entry]

        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Wrote ABS metadata.json for %s (title=%r, series=%r)", book_dir.name, title, series_entry)
    except Exception as e:
        logger.warning("Could not write metadata.json in %s: %s", book_dir, e)


async def process_download(request_id: int) -> None:
    """Runs (or resumes) the full download pipeline for a request.
    Checks the current DB state so it can skip already-completed steps."""
    async with async_session() as db:
        try:
            result = await db.execute(
                select(DownloadRequest).where(DownloadRequest.id == request_id)
            )
            req = result.scalar_one_or_none()
            if req is None:
                logger.warning("RD download: request %s not found", request_id)
                return
            # Download with the requesting user's library-group RD key
            from app.services import debrid_tokens
            await debrid_tokens.apply_tokens_for_user_id(req.user_id)
            link = req.magnet_link
            title = req.title
            media_type = req.media_type or "audiobook"
            rd_id = req.rd_torrent_id

            author, book_title = downloader.parse_torrent_name(title)
            if req.author:
                author = req.author
                if book_title == author or not book_title or book_title == "Unknown":
                    stripped = re.sub(r"\s*-\s*" + re.escape(author) + r"\s*$", "", title, flags=re.IGNORECASE).strip()
                    book_title = downloader.sanitize_filename(stripped) if stripped else downloader.sanitize_filename(title)

            if await _is_cancelled(request_id):
                return

            if not rd_id:
                await _update_status(db, request_id, "sent_to_rd", "Sending to Real-Debrid")
                # ABB detail pages are HTML — scrape the info hash into a magnet first.
                if (
                    not link.startswith("magnet:")
                    and "audiobookbay" in link.lower()
                ):
                    try:
                        from app.services import audiobookbay

                        m, _h = await audiobookbay.resolve_magnet_from_details(
                            link, title=title or ""
                        )
                        if m:
                            link = m
                            req.magnet_link = m
                            await db.commit()
                    except Exception as e:
                        logger.warning("ABB magnet resolve for request %s failed: %s", request_id, e)
                if link.startswith("magnet:"):
                    rd_result = await real_debrid.add_magnet(link)
                else:
                    rd_result = await real_debrid.add_torrent_file(link)
                rd_id = rd_result["id"]
                await _update_status(db, request_id, "sent_to_rd", rd_torrent_id=rd_id)
            else:
                logger.info(f"Request {request_id}: resuming with existing RD torrent {rd_id}")

            rd_info = await real_debrid.get_torrent_info(rd_id)
            rd_status = rd_info.get("status")

            if rd_status in ("magnet_error", "error", "virus", "dead"):
                raise RuntimeError(f"Real-Debrid torrent failed with status: {rd_status}")

            if rd_status == "waiting_files_selection":
                await real_debrid.select_files(rd_id)

            if rd_status != "downloaded":
                await _update_status(db, request_id, "downloading_rd", "Waiting for Real-Debrid to finish")

                async def _on_rd_progress(info: dict) -> None:
                    if await _is_cancelled(request_id):
                        raise RuntimeError("cancelled")
                    detail, pct, speed = _rd_progress_detail(info)
                    await _report_progress(
                        request_id,
                        req.user_id,
                        "downloading_rd",
                        detail,
                        progress_percent=pct,
                        progress_speed_bps=speed,
                    )

                try:
                    torrent_info = await real_debrid.poll_until_ready(
                        rd_id, on_progress=_on_rd_progress
                    )
                except RuntimeError as e:
                    if "cancelled" in str(e).lower():
                        return
                    raise
            else:
                logger.info(f"Request {request_id}: RD torrent already downloaded, skipping poll")
                torrent_info = rd_info

            await _update_status(db, request_id, "transferring", "Downloading to library")

            if media_type == "ebook":
                from app.services.ebook_pipeline import ebook_destination_dir
                dest_dir = ebook_destination_dir(request_id, author, book_title)
                dest_dir.mkdir(parents=True, exist_ok=True)
            else:
                dest_dir = audiobook_destination_dir(request_id, author, book_title)
                dest_dir.mkdir(parents=True, exist_ok=True)

            total_links = len(torrent_info.get("links", []))
            for i, rd_link in enumerate(torrent_info.get("links", []), 1):
                file_index = i

                async def _on_file_progress(
                    bytes_done: int,
                    total_bytes: int | None,
                    speed_bps: float,
                    *,
                    idx: int = file_index,
                    total: int = total_links,
                ) -> None:
                    if await _is_cancelled(request_id):
                        raise RuntimeError("cancelled")
                    if total_bytes and total_bytes > 0:
                        file_pct = min(100.0, bytes_done / total_bytes * 100)
                        overall = ((idx - 1) + file_pct / 100) / total * 100
                    else:
                        overall = None
                    speed_str = _format_speed(speed_bps)
                    detail = f"Downloading file {idx}/{total}"
                    if speed_str:
                        detail += f" · {speed_str}"
                    if overall is not None:
                        detail += f" · {overall:.0f}%"
                    await _report_progress(
                        request_id,
                        req.user_id,
                        "transferring",
                        detail,
                        progress_percent=overall,
                        progress_bytes=bytes_done,
                        progress_total_bytes=total_bytes,
                        progress_speed_bps=speed_bps if speed_bps > 0 else None,
                    )

                if await _is_cancelled(request_id):
                    return
                await _update_status(
                    db,
                    request_id,
                    "transferring",
                    f"Downloading file {i}/{total_links}",
                    progress_percent=((i - 1) / total_links * 100) if total_links else 0,
                )
                direct_url = await real_debrid.unrestrict_link(rd_link)
                # RD CDN / proxies occasionally drop mid-body — retry a few times.
                last_dl_err: Exception | None = None
                for attempt in range(3):
                    try:
                        await downloader.download_file(
                            direct_url, dest_dir, on_progress=_on_file_progress
                        )
                        last_dl_err = None
                        break
                    except RuntimeError as e:
                        if "cancelled" in str(e).lower():
                            return
                        last_dl_err = e
                        logger.warning(
                            "RD file download attempt %s/3 failed for request %s: %s",
                            attempt + 1,
                            request_id,
                            e,
                        )
                        if attempt < 2:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            try:
                                direct_url = await real_debrid.unrestrict_link(rd_link)
                            except Exception:
                                pass
                    except (
                        httpx.RemoteProtocolError,
                        httpx.ReadError,
                        httpx.ReadTimeout,
                        httpx.ConnectError,
                        httpx.ConnectTimeout,
                    ) as e:
                        last_dl_err = e
                        logger.warning(
                            "RD file download attempt %s/3 failed for request %s: %s",
                            attempt + 1,
                            request_id,
                            e,
                        )
                        if attempt < 2:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            try:
                                direct_url = await real_debrid.unrestrict_link(rd_link)
                            except Exception:
                                pass
                if last_dl_err is not None:
                    raise RuntimeError(
                        f"Download failed after retries: {last_dl_err}"
                    ) from last_dl_err

            if media_type == "ebook" and settings.ebook_pipeline_enabled:
                # DIY organizer: identify → embed → Author/Series/Title → Kavita.
                # Completes / quarantines / notifies inside ebook_pipeline.
                from app.services.ebook_pipeline import run_ebook_after_download
                await run_ebook_after_download(
                    request_id,
                    staging=dest_dir,
                    user_id=req.user_id,
                    title=title,
                    author=author,
                    google_volume_id=getattr(req, "google_volume_id", None),
                )
                return

            if media_type == "ebook":
                await downloader.convert_ebooks_in_dir(dest_dir)

            if media_type == "audiobook" and settings.libraforge_pipeline_enabled:
                # Hand off to LibraForge (metadata → m4b → folder forge → ABS).
                # Completes / quarantines / notifies inside forge_pipeline.
                from app.services.forge_pipeline import run_forge_after_download
                await run_forge_after_download(
                    request_id,
                    staging=dest_dir,
                    user_id=req.user_id,
                    title=title,
                    author=author,
                )
                return

            if media_type == "audiobook":
                await _update_status(db, request_id, "organizing", "Organizing audiobook files")
                series_override = None
                if _is_collection_title(book_title):
                    first_book = re.sub(r"\s*(?:Books?|Vol(?:ume)?s?)\s*1\s*[-–]\s*\d+\s*$", "", book_title, flags=re.IGNORECASE).strip()
                    if first_book:
                        try:
                            series_override = await goodreads.get_series(first_book, author)
                            if series_override:
                                logger.info(f"Goodreads series: {series_override}")
                        except Exception as e:
                            logger.debug(f"Goodreads series lookup failed: {e}")
                book_dirs = organize_audiobook_files(dest_dir, author, series_override=series_override)
                logger.info(f"Post-download: {len(book_dirs)} book dir(s) from {dest_dir.name}")

            try:
                if media_type == "ebook":
                    await kavita.scan_library()
                    kavita.invalidate_cache()
                else:
                    await audiobookshelf.scan_library()
                    await asyncio.sleep(5)
                    await audiobookshelf.remove_items_with_issues()
            except Exception as e:
                logger.warning(f"Library scan failed (non-fatal): {e}")
                try:
                    lib_name = "Kavita" if media_type == "ebook" else "Audiobookshelf"
                    await push.notify_admins(db, {
                        "type": "error",
                        "title": "Library Scan Failed",
                        "body": f"{lib_name} scan failed after downloading {title}: {e}",
                        "url": "/admin?tab=requests",
                    })
                except Exception:
                    pass

            if media_type == "audiobook":
                audiobookshelf.invalidate_cache()

            lib_name = "Kavita" if media_type == "ebook" else "Audiobookshelf"
            await _update_status(db, request_id, "completed", f"Ready in {lib_name}")

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
                logger.warning(f"Admin push notification failed (non-fatal): {e}")
            try:
                await push.notify_download_complete(req.user_id, title, lib_name, db)
            except Exception as e:
                logger.warning(f"Push notification failed (non-fatal): {e}")

        except Exception as e:
            logger.exception(f"Download pipeline failed for request {request_id}")
            err_msg = (str(e) or e.__class__.__name__)[:500]
            async with async_session() as err_db:
                await _update_status(err_db, request_id, "failed", err_msg)
                try:
                    result = await err_db.execute(
                        select(DownloadRequest).where(DownloadRequest.id == request_id)
                    )
                    failed_req = result.scalar_one_or_none()
                    if not failed_req:
                        return
                    user_result = await err_db.execute(select(User).where(User.id == failed_req.user_id))
                    user = user_result.scalar_one_or_none()
                    username = user.username if user else "Unknown"
                    await push.notify_admins(err_db, {
                        "type": "download_failed",
                        "title": "Download Failed",
                        "body": f"{title} (requested by {username}): {err_msg[:200]}",
                        "url": "/admin?tab=requests",
                    })
                except Exception:
                    logger.warning("Failed to send admin push notification")


async def process_aa_download(request_id: int) -> None:
    """Direct download pipeline for Anna's Archive results (bypasses Real-Debrid)."""
    async with async_session() as db:
        try:
            result = await db.execute(
                select(DownloadRequest).where(DownloadRequest.id == request_id)
            )
            req = result.scalar_one_or_none()
            if req is None:
                logger.warning("AA download: request %s not found", request_id)
                return
            title = req.title
            media_type = req.media_type or "audiobook"
            aa_md5 = req.rd_torrent_id  # repurposed: stores AA md5 hash

            author, book_title = downloader.parse_torrent_name(title)
            if req.author:
                author = req.author
                if book_title == author or not book_title or book_title == "Unknown":
                    stripped = re.sub(r"\s*-\s*" + re.escape(author) + r"\s*$", "", title, flags=re.IGNORECASE).strip()
                    book_title = downloader.sanitize_filename(stripped) if stripped else downloader.sanitize_filename(title)

            if await _is_cancelled(request_id):
                return

            await _update_status(db, request_id, "transferring", "Resolving Anna's Archive download...")

            download_urls = await annas_archive.get_download_urls(aa_md5)
            if not download_urls:
                raise RuntimeError("Could not find download link on Anna's Archive")

            file_ext = (req.aa_file_extension or "").strip().lstrip(".")
            if file_ext and file_ext.lower() not in ("epub", "pdf", "mobi", "azw3", "azw", "fb2", "djvu", "cbr", "cbz", "txt", "zip", "rar"):
                file_ext = ""
            suggested_filename = None
            if file_ext and book_title and book_title != "Unknown":
                safe_title = downloader.sanitize_filename(book_title)
                suggested_filename = f"{safe_title}.{file_ext}"

            if media_type == "ebook":
                from app.services.ebook_pipeline import ebook_destination_dir
                dest_dir = ebook_destination_dir(request_id, author, book_title)
                dest_dir.mkdir(parents=True, exist_ok=True)
            else:
                dest_dir = audiobook_destination_dir(request_id, author, book_title)
                dest_dir.mkdir(parents=True, exist_ok=True)

            async def _on_aa_progress(bytes_done: int, total_bytes: int | None, speed_bps: float) -> None:
                if await _is_cancelled(request_id):
                    raise RuntimeError("cancelled")
                pct = None
                if total_bytes and total_bytes > 0:
                    pct = min(100.0, bytes_done / total_bytes * 100)
                speed_str = _format_speed(speed_bps)
                detail = "Downloading file"
                if speed_str:
                    detail += f" · {speed_str}"
                if pct is not None:
                    detail += f" · {pct:.0f}%"
                await _report_progress(
                    request_id,
                    req.user_id,
                    "transferring",
                    detail,
                    progress_percent=pct,
                    progress_bytes=bytes_done,
                    progress_total_bytes=total_bytes,
                    progress_speed_bps=speed_bps if speed_bps > 0 else None,
                )

            last_page_url = ""
            last_error: Exception | None = None
            downloaded = False

            for page_url in download_urls:
                if await _is_cancelled(request_id):
                    return
                last_page_url = page_url
                source_label = "Anna's Archive"
                if "archive.org" in page_url:
                    source_label = "Internet Archive"
                elif "libgen" in page_url:
                    source_label = "Library Genesis"
                elif "slow_download" in page_url:
                    source_label = "Anna's Archive (slow)"
                elif "z-lib" in page_url.lower() or "zlib" in page_url.lower():
                    source_label = "Z-Library"
                logger.info(f"AA download: trying page URL {page_url[:80]}...")
                await _update_status(
                    db, request_id, "transferring", f"Resolving via {source_label}..."
                )
                direct_url = await annas_archive.resolve_download(page_url, media_type)
                if not direct_url:
                    continue
                # Common failure: interstitial HTML advertised as a "file" URL.
                if not await annas_archive.verify_direct_file_url(direct_url):
                    logger.warning(
                        "AA download: skipping non-file URL from %s: %s",
                        source_label,
                        direct_url[:120],
                    )
                    last_error = RuntimeError(
                        f"{source_label} resolved to HTML/interstitial instead of a file"
                    )
                    await _update_status(
                        db,
                        request_id,
                        "transferring",
                        f"Source skipped ({source_label}: not a direct file), trying another…",
                    )
                    continue
                logger.info(f"AA download: resolved to {direct_url[:120]}...")
                await _update_status(
                    db, request_id, "transferring", f"Downloading via {source_label}...", progress_percent=0
                )
                try:
                    await downloader.download_file(
                        direct_url,
                        dest_dir,
                        filename=suggested_filename,
                        on_progress=_on_aa_progress,
                    )
                    downloaded = True
                    break
                except RuntimeError as e:
                    if "cancelled" in str(e).lower():
                        return
                    last_error = e
                    logger.warning(
                        "AA download failed (%s), trying next source: %s",
                        source_label,
                        e,
                    )
                    await _update_status(
                        db,
                        request_id,
                        "transferring",
                        f"Source failed ({source_label}), trying another…",
                    )
                    continue
                except (
                    httpx.ConnectError,
                    httpx.ConnectTimeout,
                    httpx.ReadTimeout,
                    httpx.RemoteProtocolError,
                    httpx.ReadError,
                    httpx.HTTPError,
                ) as e:
                    last_error = e
                    logger.warning(
                        "AA download failed (%s), trying next source: %s",
                        source_label,
                        e,
                    )
                    await _update_status(
                        db,
                        request_id,
                        "transferring",
                        f"Source failed ({source_label}), trying another…",
                    )
                    continue

            if await _is_cancelled(request_id):
                return

            if not downloaded:
                if last_error:
                    raise RuntimeError(
                        f"All download sources failed (last error: {last_error})"
                    ) from last_error
                raise RuntimeError(
                    "Could not resolve Anna's Archive download to a direct file URL"
                    + (f" (last page: {last_page_url[:120]})" if last_page_url else "")
                )

            if media_type == "ebook" and settings.ebook_pipeline_enabled:
                from app.services.ebook_pipeline import run_ebook_after_download
                await run_ebook_after_download(
                    request_id,
                    staging=dest_dir,
                    user_id=req.user_id,
                    title=title,
                    author=author,
                    google_volume_id=getattr(req, "google_volume_id", None),
                )
                return

            if media_type == "ebook":
                await downloader.convert_ebooks_in_dir(dest_dir)

            if media_type == "audiobook" and settings.libraforge_pipeline_enabled:
                from app.services.forge_pipeline import run_forge_after_download
                await run_forge_after_download(
                    request_id,
                    staging=dest_dir,
                    user_id=req.user_id,
                    title=title,
                    author=author,
                )
                return

            if media_type == "audiobook":
                await _update_status(db, request_id, "organizing", "Organizing audiobook files")
                series_override = None
                if _is_collection_title(book_title):
                    first_book = re.sub(r"\s*(?:Books?|Vol(?:ume)?s?)\s*1\s*[-–]\s*\d+\s*$", "", book_title, flags=re.IGNORECASE).strip()
                    if first_book:
                        try:
                            series_override = await goodreads.get_series(first_book, author)
                            if series_override:
                                logger.info(f"Goodreads series: {series_override}")
                        except Exception as e:
                            logger.debug(f"Goodreads series lookup failed: {e}")
                book_dirs = organize_audiobook_files(dest_dir, author, series_override=series_override)
                logger.info(f"AA post-download: {len(book_dirs)} book dir(s) from {dest_dir.name}")

            try:
                if media_type == "ebook":
                    await kavita.scan_library()
                    kavita.invalidate_cache()
                else:
                    await audiobookshelf.scan_library()
                    await asyncio.sleep(5)
                    await audiobookshelf.remove_items_with_issues()
            except Exception as e:
                logger.warning(f"Library scan failed (non-fatal): {e}")

            if media_type == "audiobook":
                audiobookshelf.invalidate_cache()

            lib_name = "Kavita" if media_type == "ebook" else "Audiobookshelf"
            await _update_status(db, request_id, "completed", f"Ready in {lib_name}")

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
                logger.warning(f"Admin push notification failed (non-fatal): {e}")
            try:
                await push.notify_download_complete(req.user_id, title, lib_name, db)
            except Exception as e:
                logger.warning(f"Push notification failed (non-fatal): {e}")

        except Exception as e:
            if await _is_cancelled(request_id):
                return
            logger.exception(f"AA download pipeline failed for request {request_id}")
            err_msg = (str(e) or e.__class__.__name__)[:500]
            async with async_session() as err_db:
                await _update_status(err_db, request_id, "failed", err_msg)
                try:
                    result = await err_db.execute(
                        select(DownloadRequest).where(DownloadRequest.id == request_id)
                    )
                    failed_req = result.scalar_one_or_none()
                    if not failed_req:
                        return
                    user_result = await err_db.execute(select(User).where(User.id == failed_req.user_id))
                    user = user_result.scalar_one_or_none()
                    username = user.username if user else "Unknown"
                    await push.notify_admins(err_db, {
                        "type": "download_failed",
                        "title": "Download Failed",
                        "body": f"{title} (requested by {username}): {err_msg[:200]}",
                        "url": "/admin?tab=requests",
                    })
                except Exception:
                    logger.warning("Failed to send admin push notification")


RESUMABLE_STATUSES = (
    "pending",
    "sent_to_rd",
    "downloading_rd",
    "transferring",
    "organizing",
    "metadata_forge",
    "m4b_convert",
    "folder_forge",
    "finalizing",
)


def _is_aa_request(req: DownloadRequest) -> bool:
    """Anna's Archive jobs store the md5 in rd_torrent_id — must not resume via RD."""
    if req.aa_file_extension:
        return True
    if req.magnet_link.startswith("aa:"):
        return True
    if req.indexer and "anna" in req.indexer.lower():
        return True
    return False


_FORGE_RESUME = {
    "metadata_forge": "metadata",
    "organizing": "metadata",
    "m4b_convert": "m4b",
    "folder_forge": "folder",
    "finalizing": "finalize",
}


async def resume_interrupted_downloads() -> None:
    """Called on startup to resume any downloads that were in progress when the app stopped."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadRequest).where(DownloadRequest.status.in_(RESUMABLE_STATUSES))
        )
        interrupted = result.scalars().all()

    if not interrupted:
        logger.info("No interrupted downloads to resume")
        return

    logger.info(f"Resuming {len(interrupted)} interrupted download(s)")
    for req in interrupted:
        media_type = req.media_type or "audiobook"
        forge_from = _FORGE_RESUME.get(req.status)

        # Ebook DIY pipeline resume (no M4B / LibraForge)
        if (
            forge_from
            and forge_from != "m4b"
            and settings.ebook_pipeline_enabled
            and media_type == "ebook"
        ):
            from app.services.ebook_pipeline import ebook_staging_dir, run_ebook_after_download
            from app.services.forge_pipeline import resolve_staging_dir

            staging_str = (getattr(req, "staging_path", None) or "").strip()
            staging = None
            if staging_str:
                try:
                    staging = resolve_staging_dir(staging_str)
                except FileNotFoundError:
                    staging = None
            if staging is None:
                staging = ebook_staging_dir(req.id, req.title)
            if staging.is_dir():
                resume_map = {
                    "metadata": "metadata",
                    "folder": "folder",
                    "finalize": "finalize",
                }
                resume_from = resume_map.get(forge_from, "metadata")
                logger.info(
                    "  -> #%s '%s' (ebook pipeline resume from %s at %s)",
                    req.id,
                    req.title,
                    resume_from,
                    staging,
                )
                asyncio.create_task(
                    run_ebook_after_download(
                        req.id,
                        staging=staging,
                        user_id=req.user_id,
                        title=req.title,
                        author=req.author,
                        google_volume_id=getattr(req, "google_volume_id", None),
                        resume_from=resume_from,
                    )
                )
                continue

        if forge_from and settings.libraforge_pipeline_enabled and media_type == "audiobook":
            staging_str = (getattr(req, "staging_path", None) or "").strip()
            staging = Path(staging_str) if staging_str else audiobook_destination_dir(
                req.id, req.author or "Unknown", req.title
            )
            if staging.is_dir():
                logger.info(
                    "  -> #%s '%s' (LibraForge resume from %s at %s)",
                    req.id,
                    req.title,
                    forge_from,
                    staging,
                )
                from app.services.forge_pipeline import run_forge_after_download

                asyncio.create_task(
                    run_forge_after_download(
                        req.id,
                        staging=staging,
                        user_id=req.user_id,
                        title=req.title,
                        author=req.author,
                        resume_from=forge_from,
                    )
                )
                continue

        kind = "AA" if _is_aa_request(req) else "RD"
        logger.info(
            f"  -> #{req.id} '{req.title}' (status={req.status}, {kind}, id={req.rd_torrent_id})"
        )
        if _is_aa_request(req):
            asyncio.create_task(process_aa_download(req.id))
        else:
            asyncio.create_task(process_download(req.id))
