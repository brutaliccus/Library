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


def test_status_ready_with_new_dumps_banner(ol_paths):
    db, _dumps = ol_paths
    _write_minimal_catalog(db, works=5)
    status_path = db.parent / "ol_catalog_build.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "message": "Catalog ready (1.2 GB).",
                "new_dumps_available": True,
                "changed_dumps": ["works"],
            }
        ),
        encoding="utf-8",
    )
    status = ol_catalog_build.get_status()
    assert status["catalog_ready"] is True
    assert status["new_dumps_available"] is True
    assert "has not been built yet" not in status["message"]
    assert "New Open Library dumps available" in status["message"]
    assert "Catalog ready" in status["message"] or "Catalog DB ready" in status["message"]


def test_schedule_build_persists_and_cancels(ol_paths):
    import asyncio
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(hours=6)
    status = asyncio.run(
        ol_catalog_build.schedule_build(
            scheduled_at=when,
            include_editions=False,
            force_download=True,
        )
    )
    assert status["scheduled_build_at"]
    assert status["scheduled_force_download"] is True
    assert status["schedule_timezone"] == "browser_local"

    # Survives a fresh status read (same JSON file).
    again = ol_catalog_build.get_status()
    assert again["scheduled_build_at"] == status["scheduled_build_at"]

    cancelled = asyncio.run(ol_catalog_build.cancel_scheduled_build())
    assert cancelled["scheduled_build_at"] is None


def test_schedule_rejects_past(ol_paths):
    import asyncio
    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with pytest.raises(ValueError, match="future"):
        asyncio.run(ol_catalog_build.schedule_build(scheduled_at=past))


def test_tick_scheduled_build_starts_when_due(ol_paths, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone

    started: list[dict] = []

    async def _fake_start(**kwargs):
        started.append(kwargs)
        return {"status": "running", **kwargs}

    notified: list[dict] = []

    async def _notify(payload):
        notified.append(payload)

    monkeypatch.setattr(ol_catalog_build, "start_build", _fake_start)
    monkeypatch.setattr("app.services.push.notify_admins_background", _notify)

    due = datetime.now(timezone.utc) - timedelta(seconds=5)
    asyncio.run(
        ol_catalog_build.schedule_build(
            scheduled_at=due + timedelta(hours=1),  # first write a future one
            force_download=True,
        )
    )
    # Force the on-disk time into the past without going through schedule_build validation.
    path = ol_paths[0].parent / "ol_catalog_build.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scheduled_build_at"] = due.isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(data), encoding="utf-8")

    result = asyncio.run(ol_catalog_build.tick_scheduled_build())
    assert result is not None
    assert started == [
        {"include_editions": False, "skip_download": False, "force_download": True}
    ]
    assert len(notified) == 1
    assert notified[0]["type"] == "ol_dumps_scheduled_start"
    assert ol_catalog_build.get_status()["scheduled_build_at"] is None


def test_tick_scheduled_build_noop_before_due(ol_paths, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone

    async def _boom(**_kwargs):
        raise AssertionError("should not start")

    monkeypatch.setattr(ol_catalog_build, "start_build", _boom)
    when = datetime.now(timezone.utc) + timedelta(hours=2)
    asyncio.run(ol_catalog_build.schedule_build(scheduled_at=when, force_download=True))
    assert asyncio.run(ol_catalog_build.tick_scheduled_build()) is None
    assert ol_catalog_build.get_status()["scheduled_build_at"] is not None
