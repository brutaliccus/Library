#!/usr/bin/env python3
"""Merge Real-Debrid / TorBox instant-cache fields from a source app.db into a target.

Source of truth for torrent listing metadata stays on the target (laptop).
Debrid instant-cache signals are unioned by info_hash:

  - rd_cached / torbox_cached: OR (never clear a True on the target)
  - rd_debrid_id / torbox_debrid_id: fill only when target is empty
  - rd_preloaded_at / torbox_preloaded_at: fill only when target is empty
  - last_debrid_check_at: take the newer timestamp when a debrid field changed

Optionally insert source-only torrents that carry any debrid signal so the
target gains cache hits the laptop never scraped.

Examples:
  python3 scripts/merge_debrid_cache.py --source /tmp/pi-app.db --target /opt/library/data/app.db --dry-run
  python3 scripts/merge_debrid_cache.py --source /tmp/pi-app.db --target /opt/library/data/app.db
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEBRID_COLS = (
    "rd_cached",
    "torbox_cached",
    "rd_debrid_id",
    "torbox_debrid_id",
    "rd_preloaded_at",
    "torbox_preloaded_at",
    "last_debrid_check_at",
)

INSERT_COLS = (
    "info_hash",
    "title",
    "indexer",
    "size_bytes",
    "seeders",
    "media_type",
    "magnet_url",
    "download_url",
    "guid",
    "parsed_isbn",
    "title_norm",
    "author_norm",
    "first_seen_at",
    "last_seen_at",
    "last_indexer_fetch_at",
    "rd_cached",
    "torbox_cached",
    "last_debrid_check_at",
    "is_active",
    "rd_debrid_id",
    "torbox_debrid_id",
    "rd_preloaded_at",
    "torbox_preloaded_at",
)


def _truthy_bool(v) -> bool:
    return bool(v) and str(v) not in ("0", "false", "False")


def _nonempty(v) -> bool:
    return v is not None and str(v).strip() != ""


def _parse_ts(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _newer_ts(a, b):
    ta, tb = _parse_ts(a), _parse_ts(b)
    if ta is None:
        return b
    if tb is None:
        return a
    return a if ta >= tb else b


def _counts(con: sqlite3.Connection) -> dict[str, int]:
    q = """
    SELECT
      COUNT(*) AS total,
      SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active,
      SUM(CASE WHEN rd_cached=1 THEN 1 ELSE 0 END) AS rd_cached,
      SUM(CASE WHEN torbox_cached=1 THEN 1 ELSE 0 END) AS torbox_cached,
      SUM(CASE WHEN rd_cached=1 AND torbox_cached=1 THEN 1 ELSE 0 END) AS both,
      SUM(CASE WHEN rd_cached=1 OR torbox_cached=1 THEN 1 ELSE 0 END) AS rd_or_tb,
      SUM(CASE WHEN rd_debrid_id IS NOT NULL AND rd_debrid_id != '' THEN 1 ELSE 0 END) AS rd_debrid_id,
      SUM(CASE WHEN torbox_debrid_id IS NOT NULL AND torbox_debrid_id != '' THEN 1 ELSE 0 END) AS torbox_debrid_id,
      SUM(CASE WHEN rd_preloaded_at IS NOT NULL THEN 1 ELSE 0 END) AS rd_preloaded,
      SUM(CASE WHEN torbox_preloaded_at IS NOT NULL THEN 1 ELSE 0 END) AS tb_preloaded
    FROM indexer_torrents
    """
    row = con.execute(q).fetchone()
    keys = [
        "total",
        "active",
        "rd_cached",
        "torbox_cached",
        "both",
        "rd_or_tb",
        "rd_debrid_id",
        "torbox_debrid_id",
        "rd_preloaded",
        "tb_preloaded",
    ]
    return {k: int(row[i] or 0) for i, k in enumerate(keys)}


def _has_debrid_signal(row: sqlite3.Row) -> bool:
    return (
        _truthy_bool(row["rd_cached"])
        or _truthy_bool(row["torbox_cached"])
        or _nonempty(row["rd_debrid_id"])
        or _nonempty(row["torbox_debrid_id"])
        or _nonempty(row["rd_preloaded_at"])
        or _nonempty(row["torbox_preloaded_at"])
    )


def _backup_target(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = target.with_name(f"{target.name}.pre-debrid-merge-{stamp}")
    src = sqlite3.connect(str(target))
    dst = sqlite3.connect(str(out))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return out


def merge(
    source: Path,
    target: Path,
    *,
    dry_run: bool = False,
    insert_missing: bool = True,
    backup: bool = True,
) -> dict:
    if not source.is_file():
        raise SystemExit(f"source DB not found: {source}")
    if not target.is_file():
        raise SystemExit(f"target DB not found: {target}")

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tgt = sqlite3.connect(str(target))
    tgt.row_factory = sqlite3.Row
    tgt.execute("PRAGMA foreign_keys=ON")

    before = _counts(tgt)
    src_counts = _counts(src)

    # Build target map: lower(info_hash) -> id + debrid fields
    tgt_map: dict[str, sqlite3.Row] = {}
    for row in tgt.execute(
        "SELECT id, lower(info_hash) AS h, "
        + ", ".join(DEBRID_COLS)
        + " FROM indexer_torrents"
    ):
        tgt_map[row["h"]] = row

    updated = 0
    inserted = 0
    skipped_no_signal = 0
    would_update_examples: list[str] = []
    would_insert_examples: list[str] = []

    # Only pull source rows that carry debrid signal (keeps merge focused + fast).
    src_rows = src.execute(
        """
        SELECT *
        FROM indexer_torrents
        WHERE rd_cached=1
           OR torbox_cached=1
           OR (rd_debrid_id IS NOT NULL AND rd_debrid_id != '')
           OR (torbox_debrid_id IS NOT NULL AND torbox_debrid_id != '')
           OR rd_preloaded_at IS NOT NULL
           OR torbox_preloaded_at IS NOT NULL
        """
    ).fetchall()

    updates: list[tuple] = []
    inserts: list[tuple] = []

    for srow in src_rows:
        h = (srow["info_hash"] or "").lower().strip()
        if not h:
            continue
        trow = tgt_map.get(h)
        if trow is None:
            if not insert_missing:
                skipped_no_signal += 1
                continue
            inserts.append(tuple(srow[c] if c != "info_hash" else h for c in INSERT_COLS))
            if len(would_insert_examples) < 5:
                would_insert_examples.append(h)
            continue

        new_rd = 1 if (_truthy_bool(trow["rd_cached"]) or _truthy_bool(srow["rd_cached"])) else 0
        new_tb = 1 if (_truthy_bool(trow["torbox_cached"]) or _truthy_bool(srow["torbox_cached"])) else 0
        new_rd_id = trow["rd_debrid_id"] if _nonempty(trow["rd_debrid_id"]) else srow["rd_debrid_id"]
        new_tb_id = (
            trow["torbox_debrid_id"]
            if _nonempty(trow["torbox_debrid_id"])
            else srow["torbox_debrid_id"]
        )
        new_rd_pre = (
            trow["rd_preloaded_at"]
            if _nonempty(trow["rd_preloaded_at"])
            else srow["rd_preloaded_at"]
        )
        new_tb_pre = (
            trow["torbox_preloaded_at"]
            if _nonempty(trow["torbox_preloaded_at"])
            else srow["torbox_preloaded_at"]
        )

        changed = (
            new_rd != (1 if _truthy_bool(trow["rd_cached"]) else 0)
            or new_tb != (1 if _truthy_bool(trow["torbox_cached"]) else 0)
            or (new_rd_id or None) != (trow["rd_debrid_id"] or None)
            or (new_tb_id or None) != (trow["torbox_debrid_id"] or None)
            or (new_rd_pre or None) != (trow["rd_preloaded_at"] or None)
            or (new_tb_pre or None) != (trow["torbox_preloaded_at"] or None)
        )
        if not changed:
            continue

        new_check = _newer_ts(trow["last_debrid_check_at"], srow["last_debrid_check_at"])
        updates.append(
            (
                new_rd,
                new_tb,
                new_rd_id,
                new_tb_id,
                new_rd_pre,
                new_tb_pre,
                new_check,
                trow["id"],
            )
        )
        if len(would_update_examples) < 5:
            would_update_examples.append(h)

    updated = len(updates)
    inserted = len(inserts)

    backup_path = None
    if not dry_run and (updates or inserts):
        if backup:
            backup_path = _backup_target(target)
            print(f"backup={backup_path}", flush=True)
        tgt.execute("BEGIN IMMEDIATE")
        try:
            if updates:
                tgt.executemany(
                    """
                    UPDATE indexer_torrents SET
                      rd_cached=?,
                      torbox_cached=?,
                      rd_debrid_id=?,
                      torbox_debrid_id=?,
                      rd_preloaded_at=?,
                      torbox_preloaded_at=?,
                      last_debrid_check_at=?
                    WHERE id=?
                    """,
                    updates,
                )
            if inserts:
                placeholders = ",".join("?" for _ in INSERT_COLS)
                cols = ",".join(INSERT_COLS)
                tgt.executemany(
                    f"INSERT INTO indexer_torrents ({cols}) VALUES ({placeholders})",
                    inserts,
                )
            tgt.commit()
        except Exception:
            tgt.rollback()
            raise

    after = _counts(tgt) if not dry_run else None
    # For dry-run, estimate after by applying in-memory (approximate via recount if we cloned).
    if dry_run:
        # Apply to a temp copy for accurate after-counts without touching target.
        tmp = target.with_name(target.name + ".merge-dryrun-tmp")
        if tmp.exists():
            tmp.unlink()
        shutil.copy2(target, tmp)
        # Prefer online backup semantics for WAL DBs
        try:
            tmp.unlink(missing_ok=True)
            s = sqlite3.connect(str(target))
            d = sqlite3.connect(str(tmp))
            with d:
                s.backup(d)
            d.close()
            s.close()
        except Exception:
            shutil.copy2(target, tmp)
        sim = sqlite3.connect(str(tmp))
        if updates:
            sim.executemany(
                """
                UPDATE indexer_torrents SET
                  rd_cached=?,
                  torbox_cached=?,
                  rd_debrid_id=?,
                  torbox_debrid_id=?,
                  rd_preloaded_at=?,
                  torbox_preloaded_at=?,
                  last_debrid_check_at=?
                WHERE id=?
                """,
                updates,
            )
        if inserts:
            placeholders = ",".join("?" for _ in INSERT_COLS)
            cols = ",".join(INSERT_COLS)
            sim.executemany(
                f"INSERT INTO indexer_torrents ({cols}) VALUES ({placeholders})",
                inserts,
            )
        sim.commit()
        after = _counts(sim)
        sim.close()
        tmp.unlink(missing_ok=True)

    src.close()
    tgt.close()

    result = {
        "source_counts": src_counts,
        "before": before,
        "after": after,
        "updated": updated,
        "inserted": inserted,
        "source_signal_rows": len(src_rows),
        "backup": str(backup_path) if backup_path else None,
        "update_examples": would_update_examples,
        "insert_examples": would_insert_examples,
        "dry_run": dry_run,
    }
    return result


def _print_counts(label: str, c: dict[str, int]) -> None:
    print(f"{label}:", flush=True)
    for k in (
        "total",
        "active",
        "rd_cached",
        "torbox_cached",
        "both",
        "rd_or_tb",
        "rd_debrid_id",
        "torbox_debrid_id",
        "rd_preloaded",
        "tb_preloaded",
    ):
        print(f"  {k}={c[k]}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True, type=Path, help="Source app.db (e.g. Pi copy)")
    p.add_argument("--target", required=True, type=Path, help="Target app.db to update")
    p.add_argument("--dry-run", action="store_true", help="Show planned changes; do not write target")
    p.add_argument(
        "--no-insert-missing",
        action="store_true",
        help="Only update existing target rows; do not insert source-only torrents",
    )
    p.add_argument("--no-backup", action="store_true", help="Skip SQLite backup of target before write")
    args = p.parse_args(argv)

    result = merge(
        args.source,
        args.target,
        dry_run=args.dry_run,
        insert_missing=not args.no_insert_missing,
        backup=not args.no_backup,
    )
    mode = "DRY-RUN" if result["dry_run"] else "MERGE"
    print(f"=== {mode} debrid cache ===", flush=True)
    print(f"source={args.source}", flush=True)
    print(f"target={args.target}", flush=True)
    _print_counts("source", result["source_counts"])
    _print_counts("target_before", result["before"])
    print(f"source_signal_rows={result['source_signal_rows']}", flush=True)
    print(f"rows_updated={result['updated']}", flush=True)
    print(f"rows_inserted={result['inserted']}", flush=True)
    if result["update_examples"]:
        print("update_examples=" + ",".join(result["update_examples"]), flush=True)
    if result["insert_examples"]:
        print("insert_examples=" + ",".join(result["insert_examples"]), flush=True)
    if result["after"] is not None:
        _print_counts("target_after", result["after"])
        b, a = result["before"], result["after"]
        print("deltas:", flush=True)
        for k in ("rd_cached", "torbox_cached", "both", "rd_or_tb", "total"):
            print(f"  {k}: {b[k]} -> {a[k]} ({a[k]-b[k]:+d})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())