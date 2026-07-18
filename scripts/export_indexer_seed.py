#!/usr/bin/env python3
"""Build a sanitized indexer-cache seed DB from a live app.db (run on the Pi)."""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

KEEP_TABLES = {
    "indexer_torrents",
    "indexer_torrents_fts",
    "indexer_torrents_fts_data",
    "indexer_torrents_fts_idx",
    "indexer_torrents_fts_docsize",
    "indexer_torrents_fts_config",
    "catalog_torrent_matches",
    "matched_volumes",
    "volume_subjects",
    "volume_subjects_fts",
    "volume_subjects_fts_data",
    "volume_subjects_fts_idx",
    "volume_subjects_fts_docsize",
    "volume_subjects_fts_content",
    "volume_subjects_fts_config",
    "scraper_state",
}


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data/app.db")
    out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "/app/data")
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "indexer_cache_seed.db"
    gz_path = out_dir / "indexer_cache_seed.db.gz"

    if dst.exists():
        dst.unlink()
    if gz_path.exists():
        gz_path.unlink()

    print(f"source={src} size_mb={src.stat().st_size / 1024 / 1024:.2f}", flush=True)
    con = sqlite3.connect(str(src))
    con.execute("VACUUM INTO ?", (str(dst),))
    con.close()

    seed = sqlite3.connect(str(dst))
    seed.execute("PRAGMA journal_mode=DELETE")
    tables = [
        r[0]
        for r in seed.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for t in tables:
        if t not in KEEP_TABLES:
            seed.execute(f'DROP TABLE IF EXISTS "{t}"')
            print(f"dropped {t}", flush=True)
    seed.commit()
    seed.execute("VACUUM")
    seed.close()

    raw_mb = dst.stat().st_size / 1024 / 1024
    print(f"seed_raw_mb={raw_mb:.2f}", flush=True)

    with open(dst, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=9) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    gz_mb = gz_path.stat().st_size / 1024 / 1024
    print(f"seed_gz_mb={gz_mb:.2f}", flush=True)
    print(f"OUT={gz_path}", flush=True)

    c = sqlite3.connect(str(dst))
    for t in sorted(KEEP_TABLES):
        try:
            n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"count {t}={n}", flush=True)
        except Exception as e:
            print(f"count {t}=skip {e}", flush=True)
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
