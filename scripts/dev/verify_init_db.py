"""Sanity check for init_db: fresh DB and pre-Alembic DB both migrate cleanly.

Run:  python scripts/dev/verify_init_db.py
Uses temp databases; never touches data/app.db.
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CHILD = r"""
import asyncio, sqlite3, sys

async def main():
    from app.database import init_db
    await init_db()

asyncio.run(main())

db_path = sys.argv[1]
con = sqlite3.connect(db_path)
names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
con.close()
required = {"users", "indexer_torrents", "indexer_torrents_fts", "indexer_torrents_fts_ai", "alembic_version"}
missing = required - names
if missing:
    print("MISSING:", missing)
    sys.exit(1)
print("OK:", sorted(required))
"""


def run_case(label: str, prepare) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "check.db"
        prepare(db_path)
        env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{db_path.as_posix()}")
        proc = subprocess.run(
            [sys.executable, "-c", CHILD, str(db_path)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        status = "PASS" if proc.returncode == 0 else "FAIL"
        print(f"[{status}] {label}")
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            sys.exit(1)


def prepare_fresh(db_path: Path) -> None:
    pass  # nothing on disk -> init_db must create everything


def prepare_pre_alembic(db_path: Path) -> None:
    """Simulate a database created before Alembic: tables exist, no alembic_version."""
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64), hashed_password VARCHAR(128), "
        "role VARCHAR(16), is_active BOOLEAN, must_change_password BOOLEAN, created_at TIMESTAMP)"
    )
    con.commit()
    con.close()


if __name__ == "__main__":
    run_case("fresh database", prepare_fresh)
    run_case("pre-Alembic database (stamp + upgrade)", prepare_pre_alembic)
    print("init_db verification passed")
