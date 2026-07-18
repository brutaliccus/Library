"""Integration test: Alembic migrations + FTS5 torrent cache search.

Runs the real migration chain against a temp SQLite file, then exercises
upsert -> FTS-backed lookup end to end.
"""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from app.services import indexer_cache
from app.services.download_discovery import BookSearchContext

ROOT = Path(__file__).resolve().parents[1]


def _migrate(db_path: Path) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(cfg, "head")


def _fake_result(info_hash: str, title: str, media_type: str = "audiobook") -> dict:
    return {
        "title": title,
        "indexer": "AudioBook Bay",
        "size": 500 * 1024 * 1024,
        "seeders": 12,
        "mediaType": media_type,
        "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}&dn=x",
        "downloadUrl": None,
        "infoHash": info_hash,
        "guid": f"guid-{info_hash}",
    }


def _ctx(title: str, author: str = "") -> BookSearchContext:
    return BookSearchContext(
        title=title,
        subtitle="",
        author=author,
        series_name=None,
        target_index=None,
        base_title=title,
        display_title=title,
    )


@pytest.fixture()
def fts_session(tmp_path, monkeypatch):
    db_path = tmp_path / "fts_test.db"
    _migrate(db_path)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(indexer_cache, "async_session", session_factory)
    yield session_factory
    asyncio.run(engine.dispose())


def test_migrations_create_fts_table(tmp_path):
    db_path = tmp_path / "schema.db"
    _migrate(db_path)

    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        names = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")
        }
    finally:
        con.close()
    assert "indexer_torrents" in names
    assert "indexer_torrents_fts" in names
    assert {"indexer_torrents_fts_ai", "indexer_torrents_fts_ad", "indexer_torrents_fts_au"} <= names


def test_upsert_and_fts_lookup(fts_session):
    async def run():
        h1 = "a" * 40
        h2 = "b" * 40
        n = await indexer_cache.upsert_torrents(
            [
                _fake_result(h1, "Brandon Sanderson - The Way of Kings (Unabridged) M4B"),
                _fake_result(h2, "Agatha Christie - Murder on the Orient Express MP3"),
            ]
        )
        assert n == 2

        results = await indexer_cache.get_torrents_for_book(_ctx("The Way of Kings", "Brandon Sanderson"))
        titles = [r["title"] for r in results]
        assert any("Way of Kings" in t for t in titles)
        assert not any("Orient Express" in t for t in titles)

        # FTS index stays in sync through the trigger on UPDATE
        n = await indexer_cache.upsert_torrents(
            [_fake_result(h2, "Agatha Christie - Death on the Nile (Unabridged)")]
        )
        assert n == 1
        results = await indexer_cache.get_torrents_for_book(_ctx("Death on the Nile", "Agatha Christie"))
        assert any("Death on the Nile" in r["title"] for r in results)

    asyncio.run(run())


def test_fts_candidate_lookup_is_used(fts_session):
    """The FTS path itself (not the LIKE fallback) returns the row."""

    async def run():
        h = "c" * 40
        await indexer_cache.upsert_torrents(
            [_fake_result(h, "Project Hail Mary by Andy Weir Unabridged Audiobook")]
        )
        async with fts_session() as db:
            rows = await indexer_cache._candidate_rows_fts(db, "project hail mary", "andy weir", 50)
            assert len(rows) == 1
            assert rows[0].info_hash == h

            # sanity: the virtual table really has the row
            cnt = (
                await db.execute(text("SELECT count(*) FROM indexer_torrents_fts"))
            ).scalar_one()
            assert cnt == 1

    asyncio.run(run())


def test_fts_does_not_return_author_only_matches(fts_session):
    """Author must not alone surface every book by that writer."""

    async def run():
        await indexer_cache.upsert_torrents(
            [
                _fake_result("d" * 40, "Matt Dinniman - Dungeon Crawler Carl M4B"),
                _fake_result("e" * 40, "Matt Dinniman - Operation Bounce House M4B"),
            ]
        )
        results = await indexer_cache.get_torrents_for_book(
            _ctx("Operation Bounce House", "Matt Dinniman")
        )
        titles = [r["title"] for r in results]
        assert any("Operation Bounce House" in t for t in titles)
        assert not any("Dungeon Crawler Carl" in t for t in titles)

    asyncio.run(run())
