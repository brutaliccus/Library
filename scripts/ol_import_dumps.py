#!/usr/bin/env python3
"""Build a compact local Open Library catalog from the monthly data dumps.

Open Library IP-bans clients that hammer its live API. Instead of querying the
API per torrent, we download the free monthly dumps once and build a small,
indexed SQLite database on the big external drive. The scraper + store then read
that local DB and never touch openlibrary.org during normal operation.

Design constraints (this runs on a Raspberry Pi with ~3.7 GB RAM):
  * Everything is streamed from gzip line-by-line -> constant memory.
  * Rows are inserted in batches; indexes/FTS are built after the bulk load.
  * Author names are NOT held in memory; they are resolved at query time from
    the `authors` table (see app/services/ol_catalog.py).

Dump format (tab-separated, 5 columns):
    type <tab> key <tab> revision <tab> last_modified <tab> JSON

Usage (inside the app container):
    python scripts/ol_import_dumps.py                 # download + build everything
    python scripts/ol_import_dumps.py --skip-download # reuse already-downloaded dumps
    python scripts/ol_import_dumps.py --no-editions   # skip the huge ISBN dump
    python scripts/ol_import_dumps.py --limit 200000  # quick test build
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

try:
    from app.config import get_settings
    _settings = get_settings()
    DEFAULT_DB = _settings.ol_catalog_db_path
    DEFAULT_DUMPS = _settings.ol_dumps_dir
    DEFAULT_UA = _settings.open_library_user_agent
    DEFAULT_EDITIONS = _settings.ol_catalog_include_editions
except Exception:  # allow running outside the app env
    DEFAULT_DB = "/openlibrary/ol_catalog.db"
    DEFAULT_DUMPS = "/openlibrary/dumps"
    DEFAULT_UA = "LibrarySite/1.0 (+https://library.example.com)"
    DEFAULT_EDITIONS = True

DUMP_URLS = {
    "authors": "https://openlibrary.org/data/ol_dump_authors_latest.txt.gz",
    "works": "https://openlibrary.org/data/ol_dump_works_latest.txt.gz",
    "editions": "https://openlibrary.org/data/ol_dump_editions_latest.txt.gz",
}

BATCH = 5000


def log(msg: str) -> None:
    print(f"[ol-import] {msg}", flush=True)


def _download(url: str, dest: Path, ua: str) -> None:
    """Stream a dump to disk (follows the archive.org redirect)."""
    if dest.exists() and dest.stat().st_size > 1024:
        log(f"skip download (already have {dest.name}, {dest.stat().st_size/1e9:.2f} GB)")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    log(f"downloading {url}")
    start = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 10:
                pct = (done / total * 100) if total else 0
                log(f"  {done/1e9:.2f} GB{f' / {total/1e9:.2f} GB ({pct:.0f}%)' if total else ''}")
                last = now
    tmp.replace(dest)
    log(f"downloaded {dest.name} ({done/1e9:.2f} GB) in {time.time()-start:.0f}s")


def _iter_dump(path: Path, limit: int | None = None):
    """Yield parsed JSON records from a gzipped OL dump, streaming."""
    n = 0
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.split("\t", 4)
            if len(parts) < 5:
                continue
            raw = parts[4].strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except Exception:
                continue
            n += 1
            if limit and n >= limit:
                return


def _text_value(v) -> str:
    """OL description/notes are sometimes a plain string, sometimes {type,value}."""
    if isinstance(v, dict):
        return (v.get("value") or "").strip()
    if isinstance(v, str):
        return v.strip()
    return ""


def _year(v: str) -> int | None:
    if not v:
        return None
    import re
    m = re.search(r"(\d{4})", str(v))
    return int(m.group(1)) if m else None


def _connect_fresh(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        PRAGMA cache_size = -20000;  -- ~20 MB page cache

        CREATE TABLE authors (
            key   TEXT PRIMARY KEY,
            name  TEXT
        );
        CREATE TABLE works (
            key          TEXT PRIMARY KEY,
            title        TEXT,
            subtitle     TEXT,
            author_keys  TEXT,   -- JSON array of /authors/OL..A keys
            subjects     TEXT,   -- JSON array (<=10)
            description  TEXT,
            cover_id     INTEGER,
            publish_year INTEGER
        );
        CREATE TABLE isbns (
            isbn      TEXT PRIMARY KEY,
            work_key  TEXT,
            title     TEXT
        );
        CREATE VIRTUAL TABLE works_fts USING fts5(
            work_key UNINDEXED,
            title,
            subtitle,
            tokenize = 'unicode61'
        );
        CREATE VIRTUAL TABLE subjects_fts USING fts5(
            work_key UNINDEXED,
            subjects,
            tokenize = 'unicode61'
        );
        """
    )
    return conn


def _subjects_text(subjects_json: str | None) -> str:
    if not subjects_json:
        return ""
    try:
        vals = json.loads(subjects_json)
    except Exception:
        return ""
    return " ".join(s for s in vals if isinstance(s, str))


def import_authors(conn: sqlite3.Connection, path: Path, limit: int | None) -> int:
    cur = conn.cursor()
    batch: list[tuple] = []
    n = 0
    for rec in _iter_dump(path, limit):
        key = rec.get("key")
        if not key:
            continue
        name = (rec.get("name") or rec.get("personal_name") or "").strip()
        batch.append((key, name))
        if len(batch) >= BATCH:
            cur.executemany("INSERT OR IGNORE INTO authors VALUES (?,?)", batch)
            batch.clear()
        n += 1
        if n % 500000 == 0:
            log(f"  authors: {n:,}")
    if batch:
        cur.executemany("INSERT OR IGNORE INTO authors VALUES (?,?)", batch)
    conn.commit()
    log(f"authors done: {n:,}")
    return n


def import_works(conn: sqlite3.Connection, path: Path, limit: int | None) -> int:
    cur = conn.cursor()
    works_batch: list[tuple] = []
    fts_batch: list[tuple] = []
    subj_batch: list[tuple] = []
    n = 0
    for rec in _iter_dump(path, limit):
        key = rec.get("key")
        title = (rec.get("title") or "").strip()
        if not key or not title:
            continue
        subtitle = (rec.get("subtitle") or "").strip()

        author_keys = []
        for a in rec.get("authors") or []:
            if isinstance(a, dict):
                ak = a.get("author")
                if isinstance(ak, dict) and ak.get("key"):
                    author_keys.append(ak["key"])
                elif isinstance(a.get("key"), str):
                    author_keys.append(a["key"])

        subjects = [s for s in (rec.get("subjects") or []) if isinstance(s, str)][:10]
        description = _text_value(rec.get("description"))
        covers = rec.get("covers") or []
        cover_id = next((c for c in covers if isinstance(c, int) and c > 0), None)
        publish_year = _year(rec.get("first_publish_date", ""))

        works_batch.append((
            key, title, subtitle,
            json.dumps(author_keys) if author_keys else None,
            json.dumps(subjects) if subjects else None,
            description or None, cover_id, publish_year,
        ))
        fts_batch.append((key, title, subtitle))
        if subjects:
            subj_batch.append((key, " ".join(subjects)))

        if len(works_batch) >= BATCH:
            cur.executemany("INSERT OR IGNORE INTO works VALUES (?,?,?,?,?,?,?,?)", works_batch)
            cur.executemany("INSERT INTO works_fts (work_key,title,subtitle) VALUES (?,?,?)", fts_batch)
            if subj_batch:
                cur.executemany("INSERT INTO subjects_fts (work_key,subjects) VALUES (?,?)", subj_batch)
            works_batch.clear()
            fts_batch.clear()
            subj_batch.clear()
        n += 1
        if n % 500000 == 0:
            log(f"  works: {n:,}")
    if works_batch:
        cur.executemany("INSERT OR IGNORE INTO works VALUES (?,?,?,?,?,?,?,?)", works_batch)
        cur.executemany("INSERT INTO works_fts (work_key,title,subtitle) VALUES (?,?,?)", fts_batch)
        if subj_batch:
            cur.executemany("INSERT INTO subjects_fts (work_key,subjects) VALUES (?,?)", subj_batch)
    conn.commit()
    log(f"works done: {n:,}")
    return n


def import_editions(conn: sqlite3.Connection, path: Path, limit: int | None) -> int:
    cur = conn.cursor()
    batch: list[tuple] = []
    n = 0
    seen = 0
    for rec in _iter_dump(path, limit):
        works = rec.get("works") or []
        work_key = works[0].get("key") if works and isinstance(works[0], dict) else None
        title = (rec.get("title") or "").strip()
        isbns = []
        for field in ("isbn_13", "isbn_10"):
            for v in rec.get(field) or []:
                digits = "".join(c for c in str(v).upper() if c.isdigit() or c == "X")
                if len(digits) in (10, 13):
                    isbns.append(digits)
        for isbn in isbns:
            batch.append((isbn, work_key, title))
        if len(batch) >= BATCH:
            cur.executemany("INSERT OR IGNORE INTO isbns VALUES (?,?,?)", batch)
            batch.clear()
        n += 1
        seen += len(isbns)
        if n % 1000000 == 0:
            log(f"  editions: {n:,} ({seen:,} isbns)")
    if batch:
        cur.executemany("INSERT OR IGNORE INTO isbns VALUES (?,?,?)", batch)
    conn.commit()
    log(f"editions done: {n:,} editions, {seen:,} isbns")
    return n


def build_indexes(conn: sqlite3.Connection, with_editions: bool) -> None:
    log("building indexes...")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_works_year ON works(publish_year);
        """
    )
    if with_editions:
        conn.executescript(
            "CREATE INDEX IF NOT EXISTS ix_isbns_work ON isbns(work_key);"
        )
    log("optimizing works fts (this can take a while on a Pi)...")
    conn.execute("INSERT INTO works_fts(works_fts) VALUES ('optimize')")
    conn.commit()
    if _has_table(conn, "subjects_fts"):
        log("optimizing subjects fts...")
        conn.execute("INSERT INTO subjects_fts(subjects_fts) VALUES ('optimize')")
        conn.commit()
    log("running ANALYZE...")
    conn.execute("ANALYZE")
    conn.commit()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
    ).fetchone()
    return bool(row)


def build_subjects_index(db_path: Path) -> int:
    """Populate subjects_fts from an already-built works table (no dump re-parse).

    Lets us add genre browse to an existing catalog without a full rebuild.
    Operates in place on the given DB file.
    """
    if not db_path.exists():
        log(f"ERROR: DB not found: {db_path}")
        return 1
    log(f"building subjects index in {db_path}")
    start = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;"
        )
        conn.execute("DROP TABLE IF EXISTS subjects_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE subjects_fts USING fts5("
            "work_key UNINDEXED, subjects, tokenize='unicode61')"
        )
        cur = conn.cursor()
        read = conn.cursor()
        batch: list[tuple] = []
        n = 0
        added = 0
        for key, subjects_json in read.execute(
            "SELECT key, subjects FROM works WHERE subjects IS NOT NULL AND subjects <> ''"
        ):
            text = _subjects_text(subjects_json)
            if text:
                batch.append((key, text))
                added += 1
            if len(batch) >= BATCH:
                cur.executemany("INSERT INTO subjects_fts (work_key,subjects) VALUES (?,?)", batch)
                batch.clear()
            n += 1
            if n % 2000000 == 0:
                log(f"  scanned {n:,} works, indexed {added:,}")
        if batch:
            cur.executemany("INSERT INTO subjects_fts (work_key,subjects) VALUES (?,?)", batch)
        conn.commit()
        log("optimizing subjects fts...")
        conn.execute("INSERT INTO subjects_fts(subjects_fts) VALUES ('optimize')")
        conn.commit()
    finally:
        conn.close()
    log(f"subjects index done: {added:,} works in {time.time()-start:.0f}s")
    return 0


def finalize_building(
    build_path: Path,
    db_path: Path,
    *,
    with_editions: bool,
) -> int:
    """Finish indexes on an existing .building file and swap to ol_catalog.db."""
    if not build_path.exists():
        log(f"ERROR: no partial build at {build_path}")
        return 1
    log(f"finalizing existing build at {build_path} (editions={with_editions})")
    start = time.time()
    conn = sqlite3.connect(str(build_path))
    try:
        build_indexes(conn, with_editions)
    finally:
        conn.close()
    if db_path.exists():
        db_path.unlink()
    build_path.replace(db_path)
    size_gb = db_path.stat().st_size / 1e9
    log(f"DONE in {time.time()-start:.0f}s -> {db_path} ({size_gb:.2f} GB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build local Open Library catalog from dumps")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dumps", default=DEFAULT_DUMPS)
    ap.add_argument("--ua", default=DEFAULT_UA)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--no-editions", action="store_true")
    ap.add_argument("--finalize-only", action="store_true",
                    help="finish indexes on existing ol_catalog.building and swap to ol_catalog.db")
    ap.add_argument("--build-subjects", action="store_true",
                    help="(re)build subjects_fts on the existing ol_catalog.db in place")
    ap.add_argument("--limit", type=int, default=None, help="max records per dump (testing)")
    args = ap.parse_args()

    with_editions = DEFAULT_EDITIONS and not args.no_editions

    db_path = Path(args.db)
    build_path = db_path.with_suffix(".building")

    # In-place maintenance modes don't touch the dumps dir; handle them first so
    # they work even where the dumps mount is absent (e.g. host-side runs).
    if args.build_subjects:
        return build_subjects_index(db_path)
    if args.finalize_only:
        return finalize_building(build_path, db_path, with_editions=with_editions)

    dumps_dir = Path(args.dumps)
    dumps_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    wanted = ["authors", "works"] + (["editions"] if with_editions else [])
    paths = {name: dumps_dir / f"ol_dump_{name}.txt.gz" for name in wanted}

    if not args.skip_download:
        for name in wanted:
            _download(DUMP_URLS[name], paths[name], args.ua)
    for name in wanted:
        if not paths[name].exists():
            log(f"ERROR: dump missing: {paths[name]} (drop --skip-download to fetch)")
            return 1

    log(f"building catalog at {build_path} (editions={with_editions})")
    start = time.time()
    conn = _connect_fresh(build_path)
    try:
        import_authors(conn, paths["authors"], args.limit)
        import_works(conn, paths["works"], args.limit)
        if with_editions:
            import_editions(conn, paths["editions"], args.limit)
        build_indexes(conn, with_editions)
    finally:
        conn.close()

    # Atomic swap into place.
    if db_path.exists():
        db_path.unlink()
    build_path.replace(db_path)
    size_gb = db_path.stat().st_size / 1e9
    log(f"DONE in {time.time()-start:.0f}s -> {db_path} ({size_gb:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
