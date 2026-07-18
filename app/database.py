import asyncio
import logging
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _ensure_db_dir(url: str) -> None:
    if url.startswith("sqlite"):
        db_path = url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


settings = get_settings()
_ensure_db_dir(settings.database_url)

_connect_args: dict = {}
_engine_kwargs: dict = {"echo": False}
if settings.database_url.startswith("sqlite"):
    # Wait up to 30s when the scraper holds the DB during heavy ingest on a Pi.
    _connect_args["timeout"] = 30
    # SQLite + many background tasks (scraper, debrid rescan, preload, API)
    # exhausts the default QueuePool (5+10). NullPool opens/closes per checkout
    # and avoids "QueuePool limit … connection timed out" under concurrency.
    _engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(
    settings.database_url,
    connect_args=_connect_args,
    **_engine_kwargs,
)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def is_sqlite_lock_error(exc: BaseException) -> bool:
    """True for SQLite locked/busy errors (SQLAlchemy or raw sqlite3)."""
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        return True
    orig = getattr(exc, "orig", None)
    if orig is not None and orig is not exc:
        return is_sqlite_lock_error(orig)
    return False


async def run_with_sqlite_retry(coro_factory, *, attempts: int = 6, base_delay: float = 0.5):
    """Retry coroutines that hit SQLite database is locked / busy."""
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except Exception as e:
            if not is_sqlite_lock_error(e):
                raise
            last_err = e
            if attempt >= attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2**attempt))
    if last_err:
        raise last_err
    return None


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _sync_database_url() -> str:
    """Alembic runs with a synchronous driver."""
    return settings.database_url.replace("+aiosqlite", "")


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _sync_database_url())
    return cfg


def _run_migrations() -> None:
    """Upgrade the schema to head.

    Databases created before Alembic was adopted already have the baseline
    schema but no alembic_version table — stamp them at the baseline revision
    so upgrade only applies the newer migrations.
    """
    from alembic import command
    from sqlalchemy import create_engine, inspect

    sync_engine = create_engine(_sync_database_url())
    try:
        insp = inspect(sync_engine)
        pre_alembic = insp.has_table("users") and not insp.has_table("alembic_version")
    finally:
        sync_engine.dispose()

    cfg = _alembic_config()
    if pre_alembic:
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")


async def init_db():
    """Bring the database schema up to date and apply startup pragmas.

    Schema changes are managed by Alembic (see migrations/). The legacy
    create_all + add-column shim below only runs for pre-Alembic databases to
    guarantee they match the 0001 baseline before it gets stamped; the list is
    FROZEN — add new schema changes as Alembic revisions instead.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN ('users', 'alembic_version')"
            )
        )
        names = {row[0] for row in result.fetchall()}
        if "users" in names and "alembic_version" not in names:
            from app import models  # noqa: F401

            await conn.run_sync(Base.metadata.create_all)
            await _add_column_if_missing(conn, "streaming_library", "genre", "VARCHAR(128) DEFAULT ''")
            await _add_column_if_missing(conn, "download_requests", "aa_file_extension", "VARCHAR(16)")
            await _add_column_if_missing(conn, "users", "private_mode", "BOOLEAN DEFAULT 0")
            await _add_column_if_missing(conn, "download_requests", "is_private", "BOOLEAN DEFAULT 0")
            await _add_column_if_missing(conn, "stream_history", "track_position_seconds", "FLOAT DEFAULT 0")
            await _add_column_if_missing(conn, "stream_history", "hidden", "BOOLEAN DEFAULT 0")
            await _add_column_if_missing(conn, "abs_play_tracking", "hidden", "BOOLEAN DEFAULT 0")
            await _add_column_if_missing(conn, "users", "preferred_debrid", "VARCHAR(16) DEFAULT 'rd'")
            await _add_column_if_missing(conn, "stream_history", "debrid_provider", "VARCHAR(16) DEFAULT 'rd'")
            await _add_column_if_missing(conn, "streaming_library", "debrid_provider", "VARCHAR(16) DEFAULT 'rd'")
            await _add_column_if_missing(conn, "users", "library_group_id", "INTEGER")
            await _add_column_if_missing(conn, "users", "library_role", "VARCHAR(16) DEFAULT 'member'")
            await _add_column_if_missing(conn, "download_requests", "progress_percent", "REAL")
            await _add_column_if_missing(conn, "download_requests", "progress_bytes", "INTEGER")
            await _add_column_if_missing(conn, "download_requests", "progress_total_bytes", "INTEGER")
            await _add_column_if_missing(conn, "download_requests", "progress_speed_bps", "REAL")
            await _add_column_if_missing(conn, "scraper_state", "last_query", "VARCHAR(256)")
            await _add_column_if_missing(conn, "scraper_state", "last_upserted_count", "INTEGER DEFAULT 0")
            await _add_column_if_missing(conn, "scraper_state", "last_matches_created", "INTEGER DEFAULT 0")

    # Alembic's command API is synchronous — run it off the event loop.
    await asyncio.to_thread(_run_migrations)

    # First-boot warm cache from the shipped seed (no-op if already populated).
    try:
        from app.services.indexer_seed import import_indexer_seed_if_empty

        seed_result = await asyncio.to_thread(
            import_indexer_seed_if_empty, settings.database_url
        )
        if seed_result.get("imported"):
            logger.info(
                "Loaded indexer cache seed (%s torrents)",
                seed_result.get("counts", {}).get("indexer_torrents", 0),
            )
        else:
            logger.info("Indexer seed: %s", seed_result.get("reason") or "skipped")
    except Exception as e:
        logger.warning("Indexer seed import skipped: %s", e)

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))

    await _backfill_default_library_group()

    # Prefer warm matched_volumes snapshot after a seed import.
    try:
        from app.services.indexer_cache import mark_summary_ready_if_populated

        await mark_summary_ready_if_populated()
    except Exception:
        pass


async def _backfill_default_library_group():
    """Backwards compatibility: existing installs get one default library group
    (using the server env API keys) owned by the earliest admin, with every
    existing user as a member. New users onboard by creating/joining a group."""
    from sqlalchemy import select
    from app.models import LibraryGroup, User

    async with async_session() as db:
        has_group = (await db.execute(select(LibraryGroup.id).limit(1))).scalar_one_or_none()
        users = (await db.execute(select(User))).scalars().all()
        if has_group or not users:
            return

        admins = sorted([u for u in users if u.role == "admin"], key=lambda u: u.id)
        owner = admins[0] if admins else sorted(users, key=lambda u: u.id)[0]

        group = LibraryGroup(
            name="Main Library",
            owner_user_id=owner.id,
            # Empty tokens -> falls back to the env-configured server keys
            real_debrid_api_token="",
            torbox_api_token="",
        )
        db.add(group)
        await db.flush()

        for u in users:
            u.library_group_id = group.id
            if u.id == owner.id:
                u.library_role = "owner"
            elif u.role == "admin":
                u.library_role = "admin"
            else:
                u.library_role = "member"
        await db.commit()


async def _add_column_if_missing(conn, table: str, column: str, col_type: str):
    """Add a column to an existing SQLite table if it doesn't exist."""
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    columns = [row[1] for row in result.fetchall()]
    if column not in columns:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
