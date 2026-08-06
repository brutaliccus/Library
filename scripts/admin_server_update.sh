#!/usr/bin/env bash
# Admin Health server-update apply entrypoint (runs in a host-mounted sidecar).
# Invoked by the Library app via Docker Engine API — not for interactive SSH use
# (prefer scripts/update_library.sh on the host).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# Sidecar is typically root; host checkout may be uid 1000 (git "dubious ownership").
if command -v git >/dev/null 2>&1; then
  git config --global --add safe.directory "$ROOT" >/dev/null 2>&1 || true
  git config --global --add safe.directory '*' >/dev/null 2>&1 || true
fi

mkdir -p data
JOB_JSON="data/server_update_job.json"
JOB_LOG="data/server_update_job.log"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date; }

write_job() {
  local phase="$1"
  local ok="${2:-}"
  local err="${3:-}"
  local finished="${4:-}"
  if command -v python3 >/dev/null 2>&1; then
    PHASE="$phase" OK="$ok" ERR="$err" FINISHED="$finished" LOG_PATH="$JOB_LOG" \
      python3 - <<'PY'
import json, os, time
from pathlib import Path
path = Path("data/server_update_job.json")
prev = {}
if path.is_file():
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
payload = {
    **prev,
    "phase": os.environ.get("PHASE") or "unknown",
    "running": (os.environ.get("PHASE") or "") == "updating",
    "logPath": "data/server_update_job.log",
    "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
ok = os.environ.get("OK") or ""
if ok in ("0", "1", "true", "false"):
    payload["ok"] = ok in ("1", "true")
err = os.environ.get("ERR") or ""
if err:
    payload["error"] = err
elif "error" in payload and (os.environ.get("PHASE") or "") == "updating":
    payload["error"] = None
fin = os.environ.get("FINISHED") or ""
if fin:
    payload["finishedAt"] = fin
    payload["running"] = False
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
  else
    printf '{"phase":"%s","running":%s,"updatedAt":"%s"}\n' \
      "$phase" "$([[ "$phase" == "updating" ]] && echo true || echo false)" "$(ts)" \
      > "$JOB_JSON"
  fi
}

: > "$JOB_LOG"
write_job "updating"

set +e
set -o pipefail
{
  echo "[admin_server_update] containerRoot=$ROOT hostRoot=${LIBRARY_HOST_ROOT_BIND:-unknown}"
  echo "[admin_server_update] started $(ts)"
  export LIBRARY_UPDATE_YES=1
  # Required so compose resolves ./data to the host install, not the sidecar mount path.
  if [[ -z "${LIBRARY_HOST_ROOT_BIND:-}" ]]; then
    echo "[admin_server_update] WARNING: LIBRARY_HOST_ROOT_BIND unset — bind mounts may resolve incorrectly" >&2
  else
    export LIBRARY_HOST_ROOT_BIND
  fi
  bash scripts/update_library.sh --force --yes
} 2>&1 | tee -a "$JOB_LOG"
ec=${PIPESTATUS[0]}
set -e

if [[ "$ec" -eq 0 ]]; then
  write_job "succeeded" "1" "" "$(ts)"
  echo "[admin_server_update] succeeded $(ts)" | tee -a "$JOB_LOG"
  exit 0
fi

write_job "failed" "0" "update_library.sh exited $ec" "$(ts)"
echo "[admin_server_update] failed exit=$ec $(ts)" | tee -a "$JOB_LOG"
exit "$ec"