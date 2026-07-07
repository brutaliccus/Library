from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from pathlib import Path

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _ensure_db_dir(url: str) -> None:
    if url.startswith("sqlite"):
        db_path = url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


settings = get_settings()
_ensure_db_dir(settings.database_url)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(__import__("sqlalchemy").text("PRAGMA journal_mode=WAL"))
        # Lightweight migrations for new columns on existing tables
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

    await _backfill_default_library_group()


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
    from sqlalchemy import text
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
