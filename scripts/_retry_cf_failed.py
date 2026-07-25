#!/usr/bin/env python3
"""Retry Chapter Forge for failed paths from a prior batch report (internal LF URL)."""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Load ABS + LibraForge env from Library Site .env without bash
ENV_FILE = Path("/opt/stacks/Library Site/.env")
if ENV_FILE.is_file():
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in {
            "ABS_URL",
            "ABS_API_KEY",
            "ABS_LIBRARY_ID",
            "LIBRAFORGE_INTERNAL_URL",
            "LIBRAFORGE_URL",
            "LIBRARY_SITE_URL",
        }:
            os.environ.setdefault(key, val.strip().strip('"').strip("'"))

# Force internal for this retry
internal = (os.environ.get("LIBRAFORGE_INTERNAL_URL") or "").strip()
if internal:
    os.environ["LIBRAFORGE_URL"] = internal
else:
    os.environ["LIBRAFORGE_URL"] = "http://127.0.0.1:5056"

sys.path.insert(0, "/opt/stacks/Library Site/scripts")
import batch_chapter_forge as b

src = Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else "/opt/stacks/libraforge/reports/chapter-forge-batch-20260725-124726.json"
)
d = json.loads(src.read_text(encoding="utf-8"))
fails = [r for r in d["results"] if r.get("status") == "failed"]
paths = [Path(r["path"]) for r in fails if r.get("path")]
base = b.default_libraforge_url()
root = Path("/mnt/Audiobooks")
docker_root = "/audiobooks"
concurrency = 2
b.log(
    f"Retrying {len(paths)} DNS/name-resolution failures against {base} "
    f"(concurrency={concurrency})"
)
results: list[dict] = []
counts = {
    "success": 0,
    "failed": 0,
    "skipped_no_asin": 0,
    "skipped_no_audio": 0,
    "skipped_already_done": 0,
    "skipped_no_audible_chapters": 0,
    "dry_run": 0,
    "other": 0,
}


def work(folder: Path):
    row = b.process_book(
        folder,
        root=root,
        base_url=base,
        docker_root=docker_root,
        dry_run=False,
        timeout_seconds=900.0,
    )
    b.log(
        f"[retry {row['status']}] asin={row.get('asin') or '-'} "
        f"chapters={row.get('chapters_before', 0)}->"
        f"{row.get('chapters_after') or row.get('chapters', 0)} "
        f"embedded_into={row.get('embedded_into') or '-'} "
        f"{row['path']}"
        + (f" err={row['error']}" if row.get("error") else "")
    )
    return row


with ThreadPoolExecutor(max_workers=concurrency) as pool:
    futs = [pool.submit(work, p) for p in paths]
    for fut in as_completed(futs):
        row = fut.result()
        results.append(row)
        key = row["status"] if row["status"] in counts else "other"
        counts[key] = counts.get(key, 0) + 1

results.sort(key=lambda r: r.get("path") or "")
abs_scan = ""
if counts.get("success", 0) > 0:
    abs_scan = b.trigger_abs_scan(base)
    if abs_scan:
        b.log(f"Triggered ABS scan: {abs_scan}")

stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
summary = {
    "stamp": stamp,
    "root": str(root),
    "libraforge_url": base,
    "dry_run": False,
    "concurrency": concurrency,
    "force_reembed": True,
    "backend": "audible-chapters",
    "retry_of": str(src),
    "skip_success_from": "",
    "counts": counts,
    "total": len(results),
    "abs_scan": abs_scan,
    "results": results,
}
reports = Path("/opt/stacks/libraforge/reports")
out = reports / f"chapter-forge-batch-{stamp}.json"
out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
mirror = Path("/opt/stacks/Library Site/reports") / out.name
try:
    mirror.write_text(json.dumps(summary, indent=2), encoding="utf-8")
except OSError:
    pass
b.log(f"Done. success={counts['success']} failed={counts['failed']} report={out}")
print(f"RETRY_SUCCESS={counts['success']}")
print(f"RETRY_FAILED={counts['failed']}")
print(f"REPORT={out}")
missing_embed = [
    r for r in results if r.get("status") == "success" and not r.get("embedded_into")
]
print(f"SUCCESS_MISSING_EMBEDDED_INTO={len(missing_embed)}")
for r in results:
    if r.get("status") != "success":
        print("FAIL", r.get("asin"), (r.get("error") or "")[:180])
