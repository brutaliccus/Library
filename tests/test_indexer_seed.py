"""Indexer cache seed import."""
from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from app.services.indexer_seed import import_indexer_seed_if_empty


def _make_app_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE indexer_torrents (
            id INTEGER PRIMARY KEY,
            info_hash TEXT,
            title_norm TEXT,
            author_norm TEXT
        );
        CREATE VIRTUAL TABLE indexer_torrents_fts USING fts5(
            title_norm, author_norm,
            content='indexer_torrents', content_rowid='id'
        );
        CREATE TABLE catalog_torrent_matches (
            id INTEGER PRIMARY KEY,
            info_hash TEXT,
            google_volume_id TEXT
        );
        CREATE TABLE matched_volumes (
            google_volume_id TEXT PRIMARY KEY,
            title TEXT
        );
        CREATE TABLE volume_subjects (
            google_volume_id TEXT PRIMARY KEY,
            subjects TEXT,
            year INTEGER
        );
        CREATE TABLE scraper_state (
            id INTEGER PRIMARY KEY,
            last_query TEXT
        );
        """
    )
    con.commit()
    con.close()


def _make_seed_gz(path: Path) -> None:
    raw = path.with_suffix("")  # .db
    if raw.suffix != ".db":
        raw = Path(str(path).replace(".db.gz", ".db"))
    con = sqlite3.connect(str(raw))
    con.executescript(
        """
        CREATE TABLE indexer_torrents (
            id INTEGER PRIMARY KEY,
            info_hash TEXT,
            title_norm TEXT,
            author_norm TEXT
        );
        CREATE TABLE catalog_torrent_matches (
            id INTEGER PRIMARY KEY,
            info_hash TEXT,
            google_volume_id TEXT
        );
        CREATE TABLE matched_volumes (
            google_volume_id TEXT PRIMARY KEY,
            title TEXT
        );
        CREATE TABLE volume_subjects (
            google_volume_id TEXT PRIMARY KEY,
            subjects TEXT,
            year INTEGER
        );
        CREATE TABLE scraper_state (
            id INTEGER PRIMARY KEY,
            last_query TEXT
        );
        INSERT INTO indexer_torrents VALUES (1, 'abc', 'dune', 'herbert');
        INSERT INTO catalog_torrent_matches VALUES (1, 'abc', 'OL1');
        INSERT INTO matched_volumes VALUES ('OL1', 'Dune');
        INSERT INTO volume_subjects VALUES ('OL1', 'science fiction', 1965);
        INSERT INTO scraper_state VALUES (1, 'seed');
        """
    )
    con.commit()
    con.close()
    with open(raw, "rb") as fin, gzip.open(path, "wb") as fout:
        fout.write(fin.read())


def test_import_indexer_seed(tmp_path, monkeypatch):
    app_db = tmp_path / "app.db"
    seed_gz = tmp_path / "indexer_cache.db.gz"
    _make_app_db(app_db)
    _make_seed_gz(seed_gz)

    monkeypatch.setattr(
        "app.services.indexer_seed.seed_archive_candidates",
        lambda: [seed_gz],
    )

    url = f"sqlite:///{app_db.as_posix()}"
    first = import_indexer_seed_if_empty(url)
    assert first["imported"] is True
    assert first["counts"]["indexer_torrents"] == 1

    second = import_indexer_seed_if_empty(url)
    assert second["imported"] is False
    assert "already_populated" in second["reason"]
