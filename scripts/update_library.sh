#!/usr/bin/env bash
# Update an existing Library install from origin/main and rebuild the app.
#
# Run on the host (not inside the app container), from the install root:
#   cd /opt/library
#   bash scripts/update_library.sh
#
# Options:
#   --force, --yes   Allow a dirty working tree (git reset --hard). Local
#                    tracked changes are discarded; .env / media / data stay.
#                    LIBRARY_UPDATE_YES=1 has the same effect (Admin UI / CI).
#   --skip-build     Fetch/reset only (no docker compose build/up).
#   --skip-keys      Skip apply_indexer_keys.sh after restart.
#   --branch NAME    Track a different remote branch (default: main).
#
# Safe by default: refuses dirty trees unless --force. Does not touch NPM
# proxy config, media paths, or .env secrets. Honors COMPOSE_PROFILES from .env.

set -euo pipefail

FORCE=0
SKIP_BUILD=0
SKIP_KEYS=0
BRANCH="main"
REMOTE="origin"

# Non-interactive Admin / CI: LIBRARY_UPDATE_YES=1 implies --force.
if [[ "${LIBRARY_UPDATE_YES:-0}" == "1" || "${LIBRARY_UPDATE_YES:-}" == "true" ]]; then
  FORCE=1
fi

c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_red() { printf '\033[31m%s\033[0m\n' "$*"; }

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force|--yes) FORCE=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --skip-keys) SKIP_KEYS=1; shift ;;
    --branch)
      BRANCH="${2:-}"
      [[ -n "$BRANCH" ]] || { c_red "error: --branch needs a name"; exit 1; }
      shift 2
      ;;
    -h|--help) usage 0 ;;
    *)
      c_red "error: unknown option: $1"
      usage 1
      ;;
  esac
done

# Resolve install root (script lives in <root>/scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# Admin update sidecar (and some CI) run as root against a non-root checkout.
if command -v git >/dev/null 2>&1; then
  git config --global --add safe.directory "$ROOT" >/dev/null 2>&1 || true
  git config --global --add safe.directory '*' >/dev/null 2>&1 || true
fi

if [[ ! -f docker-compose.yml && ! -f compose.yml ]]; then
  c_red "error: no docker-compose.yml in $ROOT — run from the Library install root."
  exit 1
fi

if [[ ! -d .git ]]; then
  c_red "error: $ROOT is not a git checkout. Clone the repo or re-run install_library.sh."
  exit 1
fi

DOCKER="${DOCKER:-docker}"
if ! command -v "$DOCKER" >/dev/null 2>&1; then
  c_red "error: docker not found on PATH"
  exit 1
fi
if ! "$DOCKER" compose version >/dev/null 2>&1; then
  c_red "error: Docker Compose plugin required (docker compose)."
  exit 1
fi

# Prefer non-sudo when the user can talk to the daemon (docker group).
if ! "$DOCKER" info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n "$DOCKER" info >/dev/null 2>&1; then
    DOCKER="sudo -n $DOCKER"
    c_yellow "Using passwordless sudo for docker."
  else
    c_red "error: cannot talk to Docker daemon. Add your user to the docker group or fix permissions."
    exit 1
  fi
fi

# Compose file path as seen by this process (may be a container mount like /library).
COMPOSE_FILE_PATH=""
if [[ -f "$ROOT/docker-compose.yml" ]]; then
  COMPOSE_FILE_PATH="$ROOT/docker-compose.yml"
elif [[ -f "$ROOT/compose.yml" ]]; then
  COMPOSE_FILE_PATH="$ROOT/compose.yml"
fi

# Host path for relative bind-mount resolution. The Admin update sidecar mounts the
# real install (e.g. /opt/library) at /library. If compose uses cwd=/library without
# --project-directory, Docker creates host binds under /library/* (empty) instead of
# /opt/library/* — app then crash-loops with "unable to open database file".
# server_update.py sets LIBRARY_HOST_ROOT_BIND to the real host path.
COMPOSE_PROJECT_DIR="${LIBRARY_HOST_ROOT_BIND:-$ROOT}"

# When running in the Admin sidecar, ensure the host project path exists in this
# mount namespace so compose can load .env from --project-directory.
if [[ -n "${LIBRARY_HOST_ROOT_BIND:-}" && ! -e "$LIBRARY_HOST_ROOT_BIND" && -d "$ROOT" ]]; then
  mkdir -p "$(dirname "$LIBRARY_HOST_ROOT_BIND")"
  ln -sfn "$ROOT" "$LIBRARY_HOST_ROOT_BIND"
fi

compose() {
  # shellcheck disable=SC2086
  if [[ -n "${LIBRARY_HOST_ROOT_BIND:-}" && -n "$COMPOSE_FILE_PATH" ]]; then
    local env_args=()
    if [[ -f "$ROOT/.env" ]]; then
      env_args+=(--env-file "$ROOT/.env")
    elif [[ -f "$COMPOSE_PROJECT_DIR/.env" ]]; then
      env_args+=(--env-file "$COMPOSE_PROJECT_DIR/.env")
    fi
    $DOCKER compose "${env_args[@]}" -f "$COMPOSE_FILE_PATH" --project-directory "$COMPOSE_PROJECT_DIR" "$@"
  else
    $DOCKER compose "$@"
  fi
}

verify_app_data_mount() {
  local expect_root="${LIBRARY_HOST_ROOT_BIND:-$ROOT}"
  expect_root="${expect_root%/}"
  local src=""
  src="$($DOCKER inspect audiobook-request --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"
  if [[ -z "$src" ]]; then
    c_yellow "warn: could not inspect audiobook-request /app/data mount"
    return 0
  fi
  case "$src" in
    "$expect_root"|"$expect_root"/*)
      echo "  data mount: $src"
      return 0
      ;;
  esac
  c_red "error: app /app/data mounted from $src (expected under $expect_root)"
  c_red "Compose likely used a container path as project directory (sidecar remap bug)."
  return 1
}

echo "Library update"
echo "  root:   $ROOT"
if [[ -n "${LIBRARY_HOST_ROOT_BIND:-}" ]]; then
  echo "  hostRoot(bind): $LIBRARY_HOST_ROOT_BIND  (compose --project-directory)"
fi
echo "  remote: $REMOTE/$BRANCH"
echo ""

# --- git update ---
if ! git_err=$(git rev-parse --is-inside-work-tree 2>&1); then
  c_red "error: not inside a git work tree"
  [[ -n "$git_err" ]] && c_red "  $git_err"
  exit 1
fi

# Detect dirty tree (tracked changes only; untracked media/data are fine).
DIRTY=0
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  DIRTY=1
fi

if [[ "$DIRTY" -eq 1 && "$FORCE" -ne 1 ]]; then
  c_red "Refusing to update: working tree has local modifications to tracked files."
  echo ""
  git status --short --untracked-files=no | head -n 40
  echo ""
  c_yellow "Commit/stash those changes, or re-run with --force to discard them"
  c_yellow "(git reset --hard $REMOTE/$BRANCH). .env, data/, and media mounts are not in git."
  exit 2
fi

BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

c_green "[1/4] Fetching $REMOTE/$BRANCH ..."
# Shallow clones: deepen/fetch the tip without requiring a full history.
if git rev-parse --is-shallow-repository >/dev/null 2>&1 && \
   [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
  git fetch --depth 1 "$REMOTE" "$BRANCH"
else
  git fetch "$REMOTE" "$BRANCH"
fi

if ! git rev-parse --verify --quiet "$REMOTE/$BRANCH" >/dev/null; then
  c_red "error: missing $REMOTE/$BRANCH after fetch"
  exit 1
fi

if [[ "$DIRTY" -eq 1 && "$FORCE" -eq 1 ]]; then
  c_yellow "Discarding local tracked changes (--force) ..."
fi

# Align to remote tip. Handles diverged shallow clones better than plain pull.
git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -B "$BRANCH" "$REMOTE/$BRANCH"
git reset --hard "$REMOTE/$BRANCH"

AFTER="$(git rev-parse --short HEAD)"
AFTER_MSG="$(git log -1 --pretty=format:'%s')"
c_green "  HEAD: $BEFORE → $AFTER  ($AFTER_MSG)"

if [[ "$SKIP_BUILD" -eq 1 ]]; then
  c_yellow "[2/4] Skipping docker build (--skip-build)"
  c_yellow "[3/4] Skipping docker up (--skip-build)"
else
  c_green "[2/4] Building app image ..."
  # shellcheck disable=SC2086
  compose build app

  c_green "[3/4] Recreating containers (honors COMPOSE_PROFILES from .env) ..."
  # shellcheck disable=SC2086
  compose up -d
  verify_app_data_mount
fi

if [[ "$SKIP_KEYS" -eq 1 ]]; then
  c_yellow "[4/4] Skipping indexer key apply (--skip-keys)"
elif [[ -f scripts/apply_indexer_keys.sh ]]; then
  c_green "[4/4] Re-applying Jackett/Prowlarr keys (idempotent) ..."
  if ! bash scripts/apply_indexer_keys.sh; then
    c_yellow "apply_indexer_keys.sh reported a problem — Admin Overview may show Not configured."
    c_yellow "Repair: bash scripts/configure_jackett.sh --force-bundled && bash scripts/configure_prowlarr.sh --force-bundled && bash scripts/apply_indexer_keys.sh"
  fi
else
  c_yellow "[4/4] No scripts/apply_indexer_keys.sh — skipped"
fi

verify_app_data_mount

# Persist revision for Admin Health (visible inside the app container via ./data).
write_install_revision() {
  mkdir -p data
  local sha short_sha branch msg committed tracking updated
  sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  short_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$BRANCH")"
  msg="$(git log -1 --pretty=format:%s 2>/dev/null || echo "")"
  committed="$(git log -1 --pretty=format:%cI 2>/dev/null || echo "")"
  tracking="${REMOTE}/${BRANCH}"
  updated="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"
  if command -v python3 >/dev/null 2>&1; then
    SHA="$sha" SHORT="$short_sha" BRANCH_V="$branch" MSG="$msg" COMMITTED="$committed" TRACKING="$tracking" UPDATED="$updated" \
      python3 - <<'PY'
import json, os
from pathlib import Path
Path("data/install_revision.json").write_text(
    json.dumps(
        {
            "sha": os.environ.get("SHA", ""),
            "shortSha": os.environ.get("SHORT", ""),
            "branch": os.environ.get("BRANCH_V", ""),
            "message": os.environ.get("MSG", ""),
            "committedAt": os.environ.get("COMMITTED", ""),
            "tracking": os.environ.get("TRACKING", ""),
            "updatedAt": os.environ.get("UPDATED", ""),
            "source": "update_library.sh",
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  else
    printf '{\n  "sha": "%s",\n  "shortSha": "%s",\n  "branch": "%s",\n  "tracking": "%s",\n  "updatedAt": "%s",\n  "source": "update_library.sh"\n}\n' \
      "$sha" "$short_sha" "$branch" "$tracking" "$updated" > data/install_revision.json
  fi
}

write_install_revision

echo ""
c_green "Update complete."
echo "  commit:  $AFTER"
echo "  message: $AFTER_MSG"
echo ""

# Soft health summary (best-effort).
echo "Health:"
# shellcheck disable=SC2086
if compose ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null | head -n 20; then
  :
else
  # shellcheck disable=SC2086
  compose ps 2>/dev/null | head -n 20 || true
fi

APP_URL="$(grep -E '^APP_URL=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)"
APP_URL="${APP_URL:-http://127.0.0.1:8085}"
echo ""
echo "Next steps:"
echo "  - Open ${APP_URL%/}/admin  → Health"
echo "  - Logs:  cd \"$ROOT\" && docker compose logs -f app"
echo "  - Cron:  see docs/ubuntu-server-install.md#updating (optional systemd timer)"
echo ""
