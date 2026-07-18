"""Smoke tests for admin health probe helpers."""

import asyncio
from pathlib import Path

from app.services import health_checks


def test_probe_ol_catalog_missing_file(tmp_path, monkeypatch):
    missing = tmp_path / "nope.db"

    class _S:
        ol_catalog_enabled = True
        ol_catalog_db_path = str(missing)

    monkeypatch.setattr(health_checks, "get_settings", lambda: _S())
    result = asyncio.run(health_checks._probe_ol_catalog())
    assert result["configured"] is True
    assert result["connected"] is False
    assert "missing" in (result.get("error") or "").lower()


def test_probe_ol_catalog_works_table(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "ol.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE works (key TEXT)")
    con.execute("INSERT INTO works VALUES ('/works/OL1W')")
    con.commit()
    con.close()

    class _S:
        ol_catalog_enabled = True
        ol_catalog_db_path = str(db_path)

    monkeypatch.setattr(health_checks, "get_settings", lambda: _S())
    result = asyncio.run(health_checks._probe_ol_catalog())
    assert result["connected"] is True
    assert result["works"] == 1


def test_probe_nyt_unconfigured(monkeypatch):
    async def _no_key():
        return ""

    monkeypatch.setattr("app.services.nyt_books.get_api_key", _no_key)
    result = asyncio.run(health_checks._probe_nyt())
    assert result["configured"] is False
    assert result["connected"] is False
