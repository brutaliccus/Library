#!/usr/bin/env bash
# Push Jackett/Prowlarr URL+API keys from .env into the running Library app.
#
# Admin Overview "configured" reads process Settings (env at container start)
# and app_settings overrides. Writing .env alone is not enough — this
# force-recreates `app` and seeds app_settings so get_effective / Admin Config
# stay aligned.
#
# Idempotent. Exit 1 if required keys are still missing after sync.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"
cd "$ROOT"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: no .env at $ENV_FILE" >&2
  exit 1
fi

get_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

set_env_key() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    local esc
    esc="$(printf '%s' "$value" | sed -e 's/[\/&]/\\&/g')"
    sed -i.bak "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    rm -f "${ENV_FILE}.bak"
  else
    echo "${key}=${value}" >>"$ENV_FILE"
  fi
}

is_placeholder() {
  local v="${1:-}"
  [[ -z "$v" || "$v" =~ ^[Yy]our- || "$v" == "changeme" || "$v" == "placeholder" || "$v" == "change-me" ]]
}

JU="$(get_env JACKETT_URL)"
JU="${JU:-http://audiobook-jackett:9117}"
JK="$(get_env JACKETT_API_KEY)"
PU="$(get_env PROWLARR_URL)"
PU="${PU:-http://audiobook-prowlarr:9696}"
PK="$(get_env PROWLARR_API_KEY)"

# Normalize legacy compose DNS names so Admin health probes succeed from the app container.
if [[ "$JU" == *"://jackett"* && "$JU" != *"audiobook-jackett"* ]]; then
  JU="http://audiobook-jackett:9117"
fi
if [[ "$PU" == *"://prowlarr"* && "$PU" != *"audiobook-prowlarr"* ]]; then
  PU="http://audiobook-prowlarr:9696"
fi
# Host-loopback URLs are unreachable from inside the app container.
if [[ "$JU" == *"127.0.0.1"* || "$JU" == *"localhost"* ]]; then
  JU="http://audiobook-jackett:9117"
fi
if [[ "$PU" == *"127.0.0.1"* || "$PU" == *"localhost"* ]]; then
  PU="http://audiobook-prowlarr:9696"
fi

set_env_key JACKETT_URL "$JU"
set_env_key PROWLARR_URL "$PU"

missing=0
if is_placeholder "$JK"; then
  echo "error: JACKETT_API_KEY missing in .env — run: bash scripts/configure_jackett.sh --force-bundled" >&2
  missing=1
fi
if is_placeholder "$PK"; then
  echo "error: PROWLARR_API_KEY missing in .env — run: bash scripts/configure_prowlarr.sh --force-bundled" >&2
  missing=1
fi
if [[ "$missing" -eq 1 ]]; then
  exit 1
fi

DOCKER="${DOCKER:-docker}"
if ! command -v "$DOCKER" >/dev/null 2>&1; then
  echo "error: docker not found" >&2
  exit 1
fi

echo "Recreating app so Admin Overview picks up Jackett/Prowlarr keys from .env ..."
echo "  JACKETT_URL=$JU"
echo "  PROWLARR_URL=$PU"
$DOCKER compose up -d --force-recreate --no-deps app

echo "Seeding config.jackett_* / config.prowlarr_* into app_settings ..."
ready=0
for _ in $(seq 1 45); do
  if $DOCKER compose exec -T app true >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "error: app container not ready for exec after recreate" >&2
  exit 1
fi

$DOCKER compose exec -T \
  -e JACKETT_URL_SEED="$JU" \
  -e JACKETT_API_KEY_SEED="$JK" \
  -e PROWLARR_URL_SEED="$PU" \
  -e PROWLARR_API_KEY_SEED="$PK" \
  app python - <<'PY'
import asyncio
import os
import sys

async def main() -> int:
    from app.services import app_settings
    from app.services.instance_settings import apply_runtime_overrides, get_effective, invalidate_cache

    pairs = [
        ("config.jackett_url", os.environ.get("JACKETT_URL_SEED", "").strip()),
        ("config.jackett_api_key", os.environ.get("JACKETT_API_KEY_SEED", "").strip()),
        ("config.prowlarr_url", os.environ.get("PROWLARR_URL_SEED", "").strip()),
        ("config.prowlarr_api_key", os.environ.get("PROWLARR_API_KEY_SEED", "").strip()),
    ]
    for key, val in pairs:
        if not val or val.lower().startswith("your-"):
            print(f"error: refusing to seed empty/placeholder {key}", file=sys.stderr)
            return 1
        await app_settings.set_setting(key, val)
        print(f"seeded {key}")
    invalidate_cache()
    await apply_runtime_overrides()

    for key, _ in pairs:
        eff = (await get_effective(key) or "").strip()
        if not eff or eff.lower().startswith("your-"):
            print(f"error: get_effective({key!r}) still empty after seed", file=sys.stderr)
            return 1
        print(f"verified {key}")
    print("apply_runtime_overrides done")
    return 0

raise SystemExit(asyncio.run(main()))
PY

JK2="$(get_env JACKETT_API_KEY)"
PK2="$(get_env PROWLARR_API_KEY)"
if is_placeholder "$JK2" || is_placeholder "$PK2"; then
  echo "error: keys missing from .env after apply" >&2
  exit 1
fi

echo "Jackett/Prowlarr keys applied (.env + app_settings + app recreate)"
