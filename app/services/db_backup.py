"""SQLite online backup helpers for Admin Operations."""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    url = get_settings().database_url or ""
    if "sqlite" not in url:
        raise RuntimeError("Backups are only supported for SQLite")
    raw = url.split("///")[-1]
    return Path(raw).resolve()


def backup_dir() -> Path:
    return _db_path().parent / "backups"


def list_backup_files() -> list[dict]:
    root = backup_dir()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(root.glob("app-*.db.gz"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append(
            {
                "filename": p.name,
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "status": "ok",
            }
        )
    return out


def backup_targets() -> list[dict]:
    files = list_backup_files()
    latest = files[0] if files else None
    src = _db_path()
    size = src.stat().st_size if src.is_file() else 0
    return [
        {
            "id": "app.db",
            "name": "App database",
            "label": "App database",
            "source_path": str(src),
            "status": "ok" if latest else "missing",
            "ok": bool(latest),
            "last_backed_up_at": latest["created_at"] if latest else None,
            "size_bytes": latest["size_bytes"] if latest else size,
            "backup_count": len(files),
            "latest_backup": latest,
            "backup_url": "/admin/backups/app.db",
        }
    ]


def create_backup_now(*, retention_days: int = 14) -> dict:
    """Create a gzipped SQLite backup (safe while the app is writing via .backup)."""
    src = _db_path()
    if not src.is_file():
        raise FileNotFoundError(f"Database not found: {src}")
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tmp = out_dir / f"app-{stamp}.db"
    gz_path = out_dir / f"app-{stamp}.db.gz"

    conn_src = sqlite3.connect(str(src))
    conn_dst = sqlite3.connect(str(tmp))
    try:
        with conn_dst:
            conn_src.backup(conn_dst)
    finally:
        conn_dst.close()
        conn_src.close()

    with open(tmp, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    try:
        tmp.unlink()
    except OSError:
        pass

    cutoff = datetime.now(timezone.utc).timestamp() - (retention_days * 86400)
    for p in out_dir.glob("app-*.db.gz"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass

    st = gz_path.stat()
    logger.info("Backup written: %s (%s bytes)", gz_path, st.st_size)
    return {
        "filename": gz_path.name,
        "size_bytes": st.st_size,
        "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "status": "ok",
    }