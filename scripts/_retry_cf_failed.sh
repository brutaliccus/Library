#!/bin/bash
set -euo pipefail
export LIBRAFORGE_URL="http://127.0.0.1:5056"
ENV_FILE="/opt/stacks/Library Site/.env"
while IFS= read -r line; do
  case "$line" in
    ABS_URL=*|ABS_API_KEY=*|ABS_LIBRARY_ID=*) export "$line" ;;
  esac
done < "$ENV_FILE"

REPORT="/opt/stacks/libraforge/reports/chapter-forge-batch-20260725-124726.json"
python3 - <<'PY'
import json
from pathlib import Path
d=json.load(open("/opt/stacks/libraforge/reports/chapter-forge-batch-20260725-124726.json"))
fails=[r for r in d["results"] if r.get("status")=="failed"]
print("fail_count", len(fails))
paths=[]
for r in fails:
    print(r.get("asin"), r.get("path"))
    paths.append(r.get("path"))
Path("/tmp/cf_retry_paths.txt").write_text("\n".join(paths)+"\n", encoding="utf-8")
PY

# Retry via a small wrapper that only processes listed folders
python3 - <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, "/opt/stacks/Library Site/scripts")
import batch_chapter_forge as b

base = os.environ.get("LIBRAFORGE_URL", "http://127.0.0.1:5056").rstrip("/")
root = Path("/mnt/Audiobooks")
docker_root = "/audiobooks"
paths = [Path(p.strip()) for p in Path("/tmp/cf_retry_paths.txt").read_text().splitlines() if p.strip()]
print(f"Retrying {len(paths)} books against {base}")
results=[]
counts={"success":0,"failed":0,"skipped_no_audible_chapters":0,"other":0}
for folder in paths:
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
        f"chapters={row.get('chapters_before', 0)}->{row.get('chapters_after') or row.get('chapters', 0)} "
        f"{row['path']}"
        + (f" err={row['error']}" if row.get("error") else "")
    )
    results.append(row)
    key = row["status"] if row["status"] in counts else "other"
    counts[key]=counts.get(key,0)+1

# Merge into original report
orig_path = Path("/opt/stacks/libraforge/reports/chapter-forge-batch-20260725-124726.json")
orig = json.loads(orig_path.read_text(encoding="utf-8"))
by_path = {r["path"]: r for r in results}
merged=[]
for r in orig["results"]:
    if r.get("path") in by_path:
        merged.append(by_path[r["path"]])
    else:
        merged.append(r)
from collections import Counter
c=Counter(r.get("status") for r in merged)
orig["results"]=merged
orig["counts"]={
    "success": c.get("success",0),
    "failed": c.get("failed",0),
    "skipped_no_asin": c.get("skipped_no_asin",0),
    "skipped_no_audio": c.get("skipped_no_audio",0),
    "skipped_already_done": c.get("skipped_already_done",0),
    "skipped_no_audible_chapters": c.get("skipped_no_audible_chapters",0),
    "dry_run": c.get("dry_run",0),
    "other": sum(v for k,v in c.items() if k not in {
        "success","failed","skipped_no_asin","skipped_no_audio","skipped_already_done","skipped_no_audible_chapters","dry_run"
    }),
}
orig["total"]=len(merged)
orig["retry"]={
    "reason": "DNS failure against forge.library.freiverse.com; retried via 127.0.0.1:5056",
    "retried": len(results),
    "counts": counts,
}
# ABS scan again
orig["abs_scan_retry"]=b.trigger_abs_scan(base)
stamp="20260725-124726-retried"
out=Path(f"/opt/stacks/libraforge/reports/chapter-forge-batch-{stamp}.json")
out.write_text(json.dumps(orig, indent=2), encoding="utf-8")
# also overwrite main report path and mirrors
orig_path.write_text(json.dumps(orig, indent=2), encoding="utf-8")
mirror=Path("/opt/stacks/Library Site/reports")/orig_path.name
mirror.write_text(json.dumps(orig, indent=2), encoding="utf-8")
mirror2=Path("/opt/stacks/Library Site/reports")/out.name
mirror2.write_text(json.dumps(orig, indent=2), encoding="utf-8")
print("FINAL_COUNTS", orig["counts"])
print("ABS", orig.get("abs_scan_retry"))
print("OUT", out)
changed=sum(1 for r in merged if r.get("status")=="success" and int(r.get("chapters_before") or 0)!=int(r.get("chapters_after") or r.get("chapters") or 0))
numeric_before=0
for r in merged:
    if r.get("status")!="success":
        continue
    bcount=int(r.get("chapters_before") or 0)
    nb=int(r.get("numeric_before") or 0)
    if bcount and nb/bcount>=0.8:
        numeric_before+=1
print("success_count_change", changed)
print("success_mostly_numeric_before", numeric_before)
fails=[r for r in merged if r.get("status")=="failed"]
print("remaining_failed", len(fails))
for r in fails:
    print(" FAIL", r.get("asin"), (r.get("error") or "")[:200])
PY