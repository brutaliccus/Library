"""Local Open Library catalog lookups.

Reads the compact SQLite database built by scripts/ol_import_dumps.py from the
monthly Open Library data dumps. This replaces live openlibrary.org API calls
for torrent matching and book detail/synopsis lookups, so the scraper never
hammers (and gets IP-banned by) Open Library.

Returned dicts match the normalized shape used by app.services.google_books so
downstream code (catalog_match, store cards, detail pages) is source-agnostic.
Book/volume ids use the same `OL:/works/OL..W` convention as before.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import aiosqlite

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_conn: aiosqlite.Connection | None = None
_conn_path: str | None = None
_STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "for"}


def catalog_ready() -> bool:
    """True when a built catalog DB exists on disk."""
    path = settings.ol_catalog_db_path
    try:
        return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


async def _get_conn() -> aiosqlite.Connection | None:
    """Open (once) a read-only connection to the catalog DB."""
    global _conn, _conn_path
    if not catalog_ready():
        return None
    path = settings.ol_catalog_db_path
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            await _conn.close()
        except Exception:
            pass
        _conn = None
    try:
        # Read-only, immutable-ish: the importer swaps the file in atomically.
        uri = f"file:{path}?mode=ro"
        _conn = await aiosqlite.connect(uri, uri=True)
        _conn.row_factory = aiosqlite.Row
        _conn_path = path
        return _conn
    except Exception as e:
        logger.warning("ol_catalog: failed to open %s: %s", path, e)
        _conn = None
        return None


async def open_private_connection() -> aiosqlite.Connection | None:
    """Open a fresh read-only connection (caller owns + closes it).

    Used by heavy bulk jobs (e.g. the catalog relink) so they don't serialise
    behind interactive store queries on the shared module connection.
    """
    if not catalog_ready():
        return None
    path = settings.ol_catalog_db_path
    try:
        conn = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = aiosqlite.Row
        return conn
    except Exception as e:
        logger.warning("ol_catalog: failed to open private conn %s: %s", path, e)
        return None


async def close() -> None:
    global _conn, _conn_path
    if _conn is not None:
        try:
            await _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None


def _cover_urls(cover_id: int | None) -> tuple[str, str]:
    if not cover_id:
        return "", ""
    return (
        f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg",
        f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg",
    )


def _fts_query(title: str) -> str:
    """Build a safe, *selective* FTS5 MATCH string from a free-text title.

    Drops stopwords and bare numbers (years like "2014" match huge posting lists
    and make bm25 ranking crawl). Tokens are quoted and implicitly AND-ed, which
    keeps queries fast; we deliberately do NOT fall back to OR (that scans
    millions of rows and can take 20s+).
    """
    tokens = re.findall(r"[0-9a-zA-Z]+", title.lower())
    filtered = [
        t for t in tokens
        if t not in _STOPWORDS and len(t) > 1 and not t.isdigit()
    ]
    if not filtered:
        # Last resort: keep alpha tokens (still no bare numbers) so we never
        # build an empty/pathological match.
        filtered = [t for t in tokens if t.isalpha() and len(t) > 1]
    return " ".join(f'"{t}"' for t in filtered[:8])


async def _resolve_author_names(conn: aiosqlite.Connection, author_keys_json: str | None) -> list[str]:
    if not author_keys_json:
        return []
    try:
        keys = json.loads(author_keys_json)
    except Exception:
        return []
    if not keys:
        return []
    keys = keys[:3]
    placeholders = ",".join("?" for _ in keys)
    names: list[str] = []
    try:
        async with conn.execute(
            f"SELECT key, name FROM authors WHERE key IN ({placeholders})", keys
        ) as cur:
            by_key = {r["key"]: r["name"] for r in await cur.fetchall()}
        for k in keys:
            nm = (by_key.get(k) or "").strip()
            if nm:
                names.append(nm)
    except Exception:
        return []
    return names


def _work_to_book(row: aiosqlite.Row, authors: list[str], *, full: bool = False) -> dict:
    ol_key = row["key"]
    cover, cover_large = _cover_urls(row["cover_id"] if "cover_id" in row.keys() else None)
    subjects: list[str] = []
    if "subjects" in row.keys() and row["subjects"]:
        try:
            subjects = [s for s in json.loads(row["subjects"]) if isinstance(s, str)]
        except Exception:
            subjects = []
    year = row["publish_year"] if "publish_year" in row.keys() else None
    book = {
        "id": f"OL:{ol_key}",
        "volumeId": f"OL:{ol_key}",
        "title": row["title"] or "Unknown",
        "subtitle": (row["subtitle"] if "subtitle" in row.keys() else "") or "",
        "authors": authors,
        "publisher": "",
        "publishedDate": str(year) if year else "",
        "description": (row["description"] if "description" in row.keys() else "") or "",
        "pageCount": 0,
        "categories": subjects[:5],
        "mainCategory": subjects[0] if subjects else "",
        "averageRating": 0,
        "ratingsCount": 0,
        "language": "en",
        "coverUrl": cover,
        "isbn10": "",
        "isbn13": "",
        "previewLink": f"https://openlibrary.org{ol_key}" if ol_key else "",
        "infoLink": f"https://openlibrary.org{ol_key}" if ol_key else "",
    }
    if full:
        book["coverUrlLarge"] = cover_large
        book["printType"] = "BOOK"
        book["seriesName"] = ""
        book["seriesBookNumber"] = ""
    return book


async def search_by_title(
    query: str, *, limit: int = 5, offset: int = 0, conn: aiosqlite.Connection | None = None
) -> list[dict]:
    """Full-text title search against the local catalog.

    Pass ``conn`` (from :func:`open_private_connection`) to run on a dedicated
    connection instead of the shared module one — used by bulk jobs so they
    don't serialise behind interactive store queries.
    """
    if conn is None:
        conn = await _get_conn()
    if conn is None:
        return []
    q = (query or "").strip()
    if len(q) < 3:
        return []
    match = _fts_query(q)
    if not match:
        return []
    offset = max(0, offset)

    async def _run(match_expr: str) -> list[aiosqlite.Row]:
        # Pure `ORDER BY rank` lets FTS5 use its fast top-N bm25 path. Do NOT add
        # extra ORDER BY expressions (e.g. a description-preference CASE): that
        # forces SQLite to read every matched works row before LIMIT, turning a
        # ~50ms query into seconds for common tokens.
        sql = (
            "SELECT w.key, w.title, w.subtitle, w.author_keys, w.subjects, "
            "       w.description, w.cover_id, w.publish_year "
            "FROM works_fts f JOIN works w ON w.key = f.work_key "
            "WHERE works_fts MATCH ? "
            "ORDER BY rank LIMIT ? OFFSET ?"
        )
        try:
            async with conn.execute(sql, (match_expr, limit, offset)) as cur:
                return await cur.fetchall()
        except Exception as e:
            logger.debug("ol_catalog FTS query failed for %r: %s", q[:60], e)
            return []

    rows = await _run(match)
    # No OR fallback on purpose: OR-ing common tokens scans millions of works and
    # can take 20s+. A strict AND that matches nothing simply means "no hit".

    # Cheap post-sort of the small top-N: prefer covers, then synopsis, preserving
    # bm25 order within each group (keeps the SQL fast — see _run).
    rows = sorted(
        rows,
        key=lambda r: (
            0 if (r["cover_id"] if "cover_id" in r.keys() else None) else 1,
            0 if (r["description"] if "description" in r.keys() else None) else 1,
        ),
    )

    books: list[dict] = []
    for row in rows:
        authors = await _resolve_author_names(conn, row["author_keys"])
        books.append(_work_to_book(row, authors))
    return books


async def browse_by_subject(subject: str, *, limit: int = 20, offset: int = 0) -> dict:
    """Browse works tagged with a subject (genre) via the local subjects index.

    Returns {"books": [...], "totalItems": int}. Requires the subjects_fts table
    (built by the importer / build-subjects step); returns empty if absent.
    """
    conn = await _get_conn()
    if conn is None:
        return {"books": [], "totalItems": 0}
    subj = (subject or "").replace("_", " ").strip()
    if not subj:
        return {"books": [], "totalItems": 0}
    tokens = re.findall(r"[0-9a-zA-Z]+", subj.lower())
    if not tokens:
        return {"books": [], "totalItems": 0}
    match = " ".join(f'"{t}"' for t in tokens)
    offset = max(0, offset)

    sql = (
        "SELECT w.key, w.title, w.subtitle, w.author_keys, w.subjects, "
        "       w.description, w.cover_id, w.publish_year "
        "FROM subjects_fts s JOIN works w ON w.key = s.work_key "
        "WHERE subjects_fts MATCH ? AND w.cover_id IS NOT NULL "
        "ORDER BY rank LIMIT ? OFFSET ?"
    )
    try:
        async with conn.execute(sql, (match, limit, offset)) as cur:
            rows = await cur.fetchall()
    except Exception as e:
        logger.debug("ol_catalog subject browse failed for %r: %s", subj, e)
        return {"books": [], "totalItems": 0}

    books: list[dict] = []
    for row in rows:
        authors = await _resolve_author_names(conn, row["author_keys"])
        books.append(_work_to_book(row, authors))
    # We don't compute an exact total (expensive); report a page-based estimate.
    total = offset + len(books) + (limit if len(books) == limit else 0)
    return {"books": books, "totalItems": total}


async def subjects_for_works(
    keys: list[str], *, conn: aiosqlite.Connection | None = None
) -> dict[str, tuple[str, int]]:
    """Batch-resolve subject text + publish year for a list of work keys.

    ``keys`` are Open Library work keys (``/works/OL..W``); a leading ``OL:`` is
    tolerated/stripped. Returns {original_key: (subjects_text, publish_year)}
    with subjects lowercased/space-joined ("" when none) and publish_year as an
    int (0 when unknown). Used to tag the matched-volume summary so genre browse
    and real "new releases" (by actual publication date) can start from "books
    we actually have".
    """
    if not keys:
        return {}
    own = conn is None
    if own:
        conn = await _get_conn()
    if conn is None:
        return {}

    # Map normalized /works/... key -> original input key (may carry OL: prefix).
    norm_to_orig: dict[str, str] = {}
    for k in keys:
        if not k:
            continue
        norm = k[3:] if k.startswith("OL:") else k
        norm = norm if norm.startswith("/works/") else f"/works/{norm.lstrip('/')}"
        norm_to_orig[norm] = k

    out: dict[str, tuple[str, int]] = {}
    norm_keys = list(norm_to_orig.keys())
    for i in range(0, len(norm_keys), 400):
        chunk = norm_keys[i : i + 400]
        placeholders = ",".join("?" for _ in chunk)
        sql = f"SELECT key, subjects, publish_year FROM works WHERE key IN ({placeholders})"
        try:
            async with conn.execute(sql, chunk) as cur:
                rows = await cur.fetchall()
        except Exception as e:
            logger.debug("ol_catalog subjects_for_works batch failed: %s", e)
            continue
        for row in rows:
            orig = norm_to_orig.get(row["key"])
            if not orig:
                continue
            subjects: list[str] = []
            if row["subjects"]:
                try:
                    subjects = [s for s in json.loads(row["subjects"]) if isinstance(s, str)]
                except Exception:
                    subjects = []
            try:
                year = int(row["publish_year"]) if row["publish_year"] else 0
            except (ValueError, TypeError):
                year = 0
            out[orig] = (" ".join(subjects).lower(), year)
    return out


async def recent_works(*, limit: int = 20, offset: int = 0, min_year: int = 2015) -> dict:
    """Recently-published works with covers (for 'new releases')."""
    conn = await _get_conn()
    if conn is None:
        return {"books": [], "totalItems": 0}
    sql = (
        "SELECT key, title, subtitle, author_keys, subjects, description, "
        "cover_id, publish_year FROM works "
        "WHERE publish_year >= ? AND cover_id IS NOT NULL "
        "ORDER BY publish_year DESC LIMIT ? OFFSET ?"
    )
    try:
        async with conn.execute(sql, (min_year, limit, max(0, offset))) as cur:
            rows = await cur.fetchall()
    except Exception as e:
        logger.debug("ol_catalog recent_works failed: %s", e)
        return {"books": [], "totalItems": 0}
    books: list[dict] = []
    for row in rows:
        authors = await _resolve_author_names(conn, row["author_keys"])
        books.append(_work_to_book(row, authors))
    return {"books": books, "totalItems": offset + len(books) + (limit if len(books) == limit else 0)}


async def lookup_isbn(isbn: str) -> dict | None:
    """Resolve an ISBN to its work via the local editions index."""
    conn = await _get_conn()
    if conn is None or not isbn:
        return None
    digits = "".join(c for c in isbn.upper() if c.isdigit() or c == "X")
    if len(digits) not in (10, 13):
        return None
    try:
        async with conn.execute(
            "SELECT work_key, title FROM isbns WHERE isbn = ?", (digits,)
        ) as cur:
            row = await cur.fetchone()
    except Exception:
        return None
    if not row or not row["work_key"]:
        return None
    book = await get_work(row["work_key"])
    if book is not None:
        book["isbn13"] = digits if len(digits) == 13 else book.get("isbn13", "")
        book["isbn10"] = digits if len(digits) == 10 else book.get("isbn10", "")
    return book


async def get_work(ol_key: str) -> dict | None:
    """Fetch a single work by its Open Library key (`/works/OL..W`)."""
    conn = await _get_conn()
    if conn is None or not ol_key:
        return None
    key = ol_key if ol_key.startswith("/works/") else f"/works/{ol_key.lstrip('/')}"
    try:
        async with conn.execute(
            "SELECT key, title, subtitle, author_keys, subjects, description, "
            "cover_id, publish_year FROM works WHERE key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    authors = await _resolve_author_names(conn, row["author_keys"])
    return _work_to_book(row, authors, full=True)
