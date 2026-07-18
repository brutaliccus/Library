import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


def _invite_code() -> str:
    """Short, human-shareable invite code (e.g. 7Q2MKX4RB3ZD)."""
    import secrets
    import string
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


class LibraryGroup(Base):
    """A shared account group: members stream/download using the group's debrid
    API keys. Downloaded books stay shared across ALL groups — this only
    controls whose Real-Debrid/Torbox keys get used."""
    __tablename__ = "library_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    invite_code: Mapped[str] = mapped_column(String(24), unique=True, index=True, default=_invite_code)
    # Empty string = fall back to the server-wide env tokens (default library)
    real_debrid_api_token: Mapped[str] = mapped_column(Text, default="")
    torbox_api_token: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="user")  # "admin" | "user"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    private_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    # Preferred debrid provider when a torrent is cached on neither/both: "rd" | "torbox"
    preferred_debrid: Mapped[str] = mapped_column(String(16), default="rd")
    # Library group whose debrid API keys this user streams/downloads with.
    # NULL = not onboarded yet (new accounts pick create-or-join on first login).
    library_group_id: Mapped[int | None] = mapped_column(ForeignKey("library_groups.id"), nullable=True)
    # Role within the library group: "owner" | "admin" | "member"
    # (owner/admin see the invite code; owner manages members and API keys)
    library_role: Mapped[str] = mapped_column(String(16), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    download_requests: Mapped[list["DownloadRequest"]] = relationship(back_populates="user")


class AccountRequest(Base):
    __tablename__ = "account_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | denied
    token: Mapped[str] = mapped_column(String(64), unique=True, default=_uuid, index=True)
    deny_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    temp_password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DownloadRequest(Base):
    __tablename__ = "download_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str | None] = mapped_column(String(256), nullable=True)
    magnet_link: Mapped[str] = mapped_column(Text, nullable=False)
    indexer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)

    media_type: Mapped[str] = mapped_column(String(16), default="audiobook")  # audiobook | ebook | unknown

    status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending -> sent_to_rd -> downloading_rd -> transferring -> completed -> failed
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    rd_torrent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aa_file_extension: Mapped[str | None] = mapped_column(String(16), nullable=True)
    progress_percent: Mapped[float | None] = mapped_column(nullable=True)
    progress_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_speed_bps: Mapped[float | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="download_requests")


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StreamHistory(Base):
    """Tracks every RD stream a user has resolved or played."""
    __tablename__ = "stream_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str] = mapped_column(String(256), default="")
    cover_url: Mapped[str] = mapped_column(String(1024), default="")
    magnet_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Torrent id on the debrid provider (column name kept for backward compat)
    rd_torrent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Which debrid service resolved this stream: "rd" | "torbox"
    debrid_provider: Mapped[str] = mapped_column(String(16), default="rd")
    tracks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    total_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    current_track_index: Mapped[int] = mapped_column(Integer, default=0)
    # Position within the current track (seconds). Needed for accurate resume when
    # per-track durations are unknown (global progress alone can't locate the track).
    track_position_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    # Hidden from the Continue Listening shelf (progress preserved)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="resolved")
    # resolved | playing | paused | finished | error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class PushSubscription(Base):
    """Web Push subscription for a user (one user can have multiple devices)."""
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(1024), nullable=False)
    p256dh: Mapped[str] = mapped_column(String(256), nullable=False)
    auth: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ABSPlayTracking(Base):
    """Tracks which ABS items a user has played, for per-user Continue Listening."""
    __tablename__ = "abs_play_tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    abs_item_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    author: Mapped[str] = mapped_column(String(256), default="")
    # Hidden from the Continue Listening shelf (ABS progress preserved)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    last_played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StreamingLibraryItem(Base):
    __tablename__ = "streaming_library"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    google_volume_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str] = mapped_column(String(256), default="")
    cover_url: Mapped[str] = mapped_column(String(1024), default="")
    genre: Mapped[str] = mapped_column(String(128), default="")
    magnet_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    rd_torrent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    debrid_provider: Mapped[str] = mapped_column(String(16), default="rd")
    stream_status: Mapped[str] = mapped_column(String(32), default="added")
    # added -> resolving -> cached -> ready -> error
    progress_seconds: Mapped[float] = mapped_column(default=0.0)
    total_seconds: Mapped[float] = mapped_column(default=0.0)
    tracks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class IndexerTorrent(Base):
    """Pre-scraped torrent listing from trusted Prowlarr indexers (DMM-style cache)."""
    __tablename__ = "indexer_torrents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    info_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    indexer: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seeders: Mapped[int] = mapped_column(Integer, default=0)
    media_type: Mapped[str] = mapped_column(String(16), default="unknown")
    magnet_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    guid: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parsed_isbn: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    title_norm: Mapped[str] = mapped_column(String(512), default="", index=True)
    author_norm: Mapped[str] = mapped_column(String(256), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    last_indexer_fetch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    rd_cached: Mapped[bool] = mapped_column(Boolean, default=False)
    torbox_cached: Mapped[bool] = mapped_column(Boolean, default=False)
    last_debrid_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rd_debrid_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    torbox_debrid_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rd_preloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    torbox_preloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CatalogTorrentMatch(Base):
    """Precomputed link between a Google Books volume and a cached torrent."""
    __tablename__ = "catalog_torrent_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    google_volume_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    info_hash: Mapped[str] = mapped_column(String(64), ForeignKey("indexer_torrents.info_hash"), nullable=False, index=True)
    match_method: Mapped[str] = mapped_column(String(16), default="fuzzy")
    match_tier: Mapped[str] = mapped_column(String(16), default="weak")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AppSetting(Base):
    """Key-value store for admin-tunable runtime settings (JSON-encoded values)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AvailabilityAlert(Base):
    """User watch for a catalog book that is not yet in the indexer cache."""
    __tablename__ = "availability_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    google_volume_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    author: Mapped[str] = mapped_column(String(256), default="")
    cover_url: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScraperState(Base):
    """Singleton progress row for the background indexer scraper."""
    __tablename__ = "scraper_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    last_query_index: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    torrents_total: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_debrid_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_query: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_upserted_count: Mapped[int] = mapped_column(Integer, default=0)
    last_matches_created: Mapped[int] = mapped_column(Integer, default=0)
    last_rss_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rss_upserted: Mapped[int] = mapped_column(Integer, default=0)
