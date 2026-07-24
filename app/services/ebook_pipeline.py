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

# Non-dot folder — Kavita is configured to ignore ``unorganized`` (see docs/ebooks.md).
EBOOK_UNORGANIZED_DIRNAME = "unorganized"
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
    return Path(settings.ebook_dir) / EBOOK_UNORGANIZED_DIRNAME


def ensure_ebook_unorganized_root() -> Path:
    """Create ``unorganized`` under the ebook library (Kavita excludes this name)."""
    root = ebook_unorganized_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Belt-and-suspenders marker; Kavita folder exclusion is the real gate.
        ignore = root / ".ignore"
        if not ignore.exists():
            ignore.write_text("", encoding="utf-8")
    except OSError as e:
        logger.warning("Could not ensure ebook unorganized root %s: %s", root, e)
    return root


def ebook_staging_dir(request_id: int, title: str) -> Path:
    """Per-request landing folder under ``/ebooks/unorganized``."""
    slug = downloader.sanitize_filename(title or "book")[:80] or "book"
    return ensure_ebook_unorganized_root() / f"req_{request_id}_{slug}"


def staging_path_for_storage(staging: Path) -> str:
    """POSIX-style path as seen in Docker (``/ebooks/unorganized/...``)."""
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


async def identify_ebook_metadata(
    *,
    staging: Path,
    title_hint: str,
    author_hint: str,
    google_volume_id: str | None = None,
) -> EbookMeta:
    """Resolve catalog metadata with confidence score.

    Order: request catalog volume → ISBN (OL / Google / ISBNdb) → title+author (Hardcover).
    """
    from app.services import google_books, hardcover, isbndb, ol_catalog

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

    # 3) Title + author → Hardcover
    search_title = hint_title
    # Strip common torrent junk for search
    search_title = re.sub(
        r"\s*[\[(](?:epub|pdf|mobi|azw3?|kindle|retail|converted)[\])]|\.(?:epub|pdf|mobi)$",
        "",
        search_title,
        flags=re.IGNORECASE,
    ).strip() or hint_title

    if search_title and await hardcover.get_api_key():
        try:
            hits = await hardcover.search_books(f"{search_title} {hint_author}".strip(), limit=8)
        except Exception as e:
            logger.warning("Hardcover ebook search failed: %s", e)
            hits = []

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

        ranked.sort(key=lambda x: x[0], reverse=True)
        if ranked:
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
            meta = EbookMeta(
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
            return meta

    # Fallback: use request hints at low confidence → quarantine
    return EbookMeta(
        title=hint_title or "Unknown",
        author=hint_author or "Unknown",
        score=0.2,
        source="hint",
        reason="No catalog/ISBN/Hardcover match",
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


async def embed_ebook_metadata(ebook_path: Path, meta: EbookMeta) -> bool:
    """Write title/author/series (OPF) via Calibre ``ebook-meta`` for Kavita series grouping."""
    if ebook_path.suffix.lower() not in {".epub", ".mobi", ".azw3", ".azw"}:
        return False
    ebook_meta = _get_ebook_meta_bin()
    if not ebook_meta:
        logger.warning("ebook-meta not found — skipping OPF embed for %s", ebook_path.name)
        return False

    cmd = [
        ebook_meta,
        str(ebook_path),
        "--title",
        meta.title or ebook_path.stem,
        "--authors",
        meta.author or "Unknown",
    ]
    if meta.series:
        cmd.extend(["--series", meta.series])
        if meta.series_index:
            cmd.extend(["--index", str(meta.series_index)])
    isbn = meta.isbn13 or meta.isbn10
    if isbn:
        cmd.extend(["--isbn", isbn])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "ebook-meta failed for %s: %s",
            ebook_path.name,
            stderr.decode(errors="replace")[:400],
        )
        return False
    logger.info("Embedded ebook metadata into %s (series=%r)", ebook_path.name, meta.series)
    return True


def organize_ebook_files(staging: Path, meta: EbookMeta) -> Path:
    """Move primary ebook into final library layout; return destination file path."""
    primary = pick_primary_ebook(staging)
    if not primary:
        raise FileNotFoundError(f"No ebook files in staging: {staging}")

    dest_file = final_ebook_path(meta, suffix=primary.suffix.lower())
    dest_dir = dest_file.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Avoid clobbering an existing library file with a different request.
    if dest_file.exists():
        stem = dest_file.stem
        n = 2
        while True:
            candidate = dest_dir / f"{stem} ({n}){dest_file.suffix}"
            if not candidate.exists():
                dest_file = candidate
                break
            n += 1

    shutil.move(str(primary), str(dest_file))
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
) -> None:
    """Post-download ebook pipeline: metadata → organize → Kavita finalize.

    ``resume_from``: ``metadata`` | ``folder`` | ``finalize``
    (no M4B step — ebooks never call LibraForge).
    """
    p = _pipeline()
    staging = Path(staging)
    await _persist_staging(request_id, staging)

    if await p._is_cancelled(request_id):
        return

    # Convert MOBI/AZW → EPUB in staging before identify/embed.
    try:
        await downloader.convert_ebooks_in_dir(staging)
    except Exception as e:
        logger.warning("Ebook convert in staging failed (continuing): %s", e)

    meta: EbookMeta | None = None

    if resume_from in ("metadata",):
        async with async_session() as db:
            await p._update_status(db, request_id, "metadata_forge", "Identifying ebook metadata…")

        meta = await identify_ebook_metadata(
            staging=staging,
            title_hint=title,
            author_hint=author or "",
            google_volume_id=google_volume_id,
        )
        min_score = float(settings.ebook_min_score)
        if meta.ambiguous or meta.score < min_score:
            reason = meta.reason or f"Score {meta.score:.2f} below minimum {min_score:.2f}"
            if meta.ambiguous:
                reason = meta.reason or "Ambiguous metadata matches"
            await _set_quarantine(request_id, reason, staging)
            return

        # Embed OPF tags on primary ebook while still in staging
        primary = pick_primary_ebook(staging)
        if primary:
            await embed_ebook_metadata(primary, meta)

    if await p._is_cancelled(request_id):
        return

    if resume_from in ("metadata", "folder"):
        if meta is None:
            # Continue-after-review: re-identify but skip score gate (admin approved).
            meta = await identify_ebook_metadata(
                staging=staging,
                title_hint=title,
                author_hint=author or "",
                google_volume_id=google_volume_id,
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

        # Refresh cover on the request if we found one
        if meta.cover_url:
            async with async_session() as db:
                result = await db.execute(
                    select(DownloadRequest).where(DownloadRequest.id == request_id)
                )
                req = result.scalar_one_or_none()
                if req and not req.cover_url:
                    req.cover_url = meta.cover_url[:1024]
                    await db.commit()

        logger.info("Ebook organized for request %s → %s", request_id, dest_file)

    if await p._is_cancelled(request_id):
        return

    # Finalize — Kavita scan
    async with async_session() as db:
        await p._update_status(db, request_id, "finalizing", "Scanning Kavita…")

    try:
        await kavita.scan_library()
        kavita.invalidate_cache()
    except Exception as e:
        logger.warning("Kavita scan after ebook organize failed (non-fatal): %s", e)
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
