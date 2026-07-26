"""Status messaging for the Open Library catalog Admin panel."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services import ol_catalog_build


def _write_minimal_catalog(db_path: Path, *, works: int = 10, analyze: bool = True) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE authors (key TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE works (
            key TEXT PRIMARY KEY,
            title TEXT,
            subtitle TEXT,
            author_keys TEXT,
            subjects TEXT,
            description TEXT,
            cover_id INTEGER,
            publish_year INTEGER
        );
        CREATE TABLE isbns (isbn TEXT PRIMARY KEY, work_key TEXT, title TEXT);
        """
    )
    for i in range(works):
        conn.execute(
            "INSERT INTO works (key, title) VALUES (?, ?)",
            (f"/works/OL{i}W", f"Title {i}"),
        )
        conn.execute(
            "INSERT INTO authors (key, name) VALUES (?, ?)",
            (f"/authors/OL{i}A", f"Author {i}"),
        )
    # Pad past the 1 MB readiness threshold without millions of rows.
    conn.execute("CREATE TABLE _pad (b BLOB)")
    conn.execute("INSERT INTO _pad (b) VALUES (?)", (b"x" * (1024 * 1024 + 100),))
    if analyze:
        conn.execute("ANALYZE")
    conn.commit()
    conn.close()


@pytest.fixture
def ol_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "ol_catalog.db"
    dumps = tmp_path / "dumps"
    dumps.mkdir()

    class _Settings:
        ol_catalog_db_path = str(db)
        ol_dumps_dir = str(dumps)

    monkeypatch.setattr(ol_catalog_build, "get_settings", lambda: _Settings())
    return db, dumps


def test_status_not_ready_without_db_or_dumps(ol_paths):
    status = ol_catalog_build.get_status()
    assert status["catalog_ready"] is False
    assert status["dumps_present"] is False
    assert status["status"] == "idle"
    assert "has not been built yet" in status["message"]


def test_status_dumps_only_not_catalog_ready(ol_paths):
    _db, dumps = ol_paths
    (dumps / "ol_dump_works_latest.txt.gz").write_bytes(b"x" * (2 * 1024 * 1024))
    status = ol_catalog_build.get_status()
    assert status["catalog_ready"] is False
    assert status["dumps_present"] is True
    assert status["dumps_size_bytes"] > 0
    assert "Dumps present" in status["message"]
    assert "catalog DB has not been built yet" in status["message"]


def test_status_ready_db_without_job_json(ol_paths):
    db, _dumps = ol_paths
    _write_minimal_catalog(db, works=12)
    status = ol_catalog_build.get_status()
    assert status["catalog_ready"] is True
    assert status["status"] == "ready"
    assert status["catalog_size_bytes"] > 1024 * 1024
    assert "has not been built yet" not in status["message"]
    assert "Catalog DB ready" in status["message"]
    assert status["catalog_works"] == 12
    assert status["catalog_authors"] == 12


def test_status_stale_job_message_overridden_when_db_ready(ol_paths):
    db, _dumps = ol_paths
    _write_minimal_catalog(db, works=5)
    status_path = db.parent / "ol_catalog_build.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "idle",
                "message": "Open Library catalog has not been built yet.",
            }
        ),
        encoding="utf-8",
    )
    status = ol_catalog_build.get_status()
    assert status["catalog_ready"] is True
    assert "has not been built yet" not in status["message"]
    assert "Catalog DB ready" in status["message"]


def test_tiny_db_not_ready(ol_paths):
    db, _dumps = ol_paths
    db.write_bytes(b"not-a-real-catalog")
    status = ol_catalog_build.get_status()
    assert status["catalog_ready"] is False
    assert status["catalog_size_bytes"] > 0
