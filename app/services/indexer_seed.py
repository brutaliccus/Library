"""Import the shipped indexer-cache seed into an empty app database.

The seed (`seed/indexer_cache.db.gz`) is a sanitized SQLite snapshot of torrent
cache + catalog match tables (no users, tokens, or settings). On first boot,
when `indexer_torrents` is empty, we decompress and load it so new installs
start with a warm cache.
"""
from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Data tables only — FTS is rebuilt after import (triggers / explicit insert).
_SEED_TABLES = (
    "indexer_torrents",
    "catalog_torrent_matches",
    "matched_volumes",
    "volume_subjects",
    "scraper_state",
)


def seed_archive_candidates() -> list[Path]:
    """Locations checked for the shipped seed archive."""
    return [
        _PROJECT_ROOT / "seed" / "indexer_cache.db.gz",
        Path("/app/seed/indexer_cache.db.gz"),
    ]


def find_seed_archive() -> Path | None:
    for p in seed_archive_candidates():
        try:
            if p.is_file() and p.stat().st_size > 64:
                return p
        except OSError:
            continue
    return None


def _app_db_path(database_url: str) -> Path | None:
    if "sqlite" not in database_url:
        return None
    raw = database_url.split("///")[-1]
    return Path(raw)


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return -1


def _copy_table(conn: sqlite3.Connection, table: str) -> int:
    cols = [
        r[1]
        for r in conn.execute(f'PRAGMA seed.table_info("{table}")').fetchall()
    ]
    if not cols:
        return 0
    col_list = ", ".join(f'"{c}"' for c in cols)
    conn.execute(
        f'INSERT OR REPLACE INTO main."{table}" ({col_list}) '
        f'SELECT {col_list} FROM seed."{table}"'
    )
    return _table_count(conn, table)


def import_indexer_seed_if_empty(database_url: str) -> dict:
    """Load seed when the torrent cache is empty. Safe to call on every boot."""
    result = {
        "imported": False,
        "skipped": True,
        "reason": "",
        "seed": None,
        "counts": {},
    }
    db_path = _app_db_path(database_url)
    if db_path is None:
        result["reason"] = "not_sqlite"
        return result
    if not db_path.exists():
        result["reason"] = "app_db_missing"
        return result

    seed_gz = find_seed_archive()
    if seed_gz is None:
        result["reason"] = "seed_missing"
        return result
    result["seed"] = str(seed_gz)

    conn = sqlite3.connect(str(db_path))
    try:
        existing = _table_count(conn, "indexer_torrents")
        if existing < 0:
            result["reason"] = "indexer_torrents_missing"
            return result
        if existing > 0:
            result["reason"] = f"already_populated ({existing} rows)"
            return result

        result["skipped"] = False
        with tempfile.TemporaryDirectory(prefix="indexer-seed-") as tmp:
            seed_db = Path(tmp) / "indexer_cache.db"
            logger.info("Decompressing indexer seed %s → %s", seed_gz, seed_db)
            with gzip.open(seed_gz, "rb") as fin, open(seed_db, "wb") as fout:
                shutil.copyfileobj(fin, fout, length=1024 * 1024)

            conn.execute("ATTACH DATABASE ? AS seed", (str(seed_db),))
            try:
                conn.execute("PRAGMA foreign_keys=OFF")
                for table in _SEED_TABLES:
                    n = _copy_table(conn, table)
                    result["counts"][table] = n
                    logger.info("Indexer seed: %s → %s rows", table, n)
                conn.commit()
            finally:
                conn.execute("DETACH DATABASE seed")

        # Rebuild FTS indexes from imported content tables.
        try:
            conn.execute(
                "INSERT INTO indexer_torrents_fts(indexer_torrents_fts) VALUES('rebuild')"
            )
        except sqlite3.Error as e:
            logger.warning("indexer_torrents_fts rebuild skipped: %s", e)
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS volume_subjects_fts "
                "USING fts5(google_volume_id UNINDEXED, subjects, tokenize='unicode61')"
            )
            conn.execute("DELETE FROM volume_subjects_fts")
            conn.execute(
                "INSERT INTO volume_subjects_fts (google_volume_id, subjects) "
                "SELECT google_volume_id, subjects FROM volume_subjects "
                "WHERE subjects IS NOT NULL AND TRIM(subjects) != ''"
            )
        except sqlite3.Error as e:
            logger.warning("volume_subjects_fts rebuild skipped: %s", e)
        conn.commit()
        result["imported"] = True
        result["reason"] = "ok"
        logger.info(
            "Indexer cache seed imported (%s torrents)",
            result["counts"].get("indexer_torrents", 0),
        )
        return result
    finally:
        conn.close()
