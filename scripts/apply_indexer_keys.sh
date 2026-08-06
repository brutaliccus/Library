#!/usr/bin/env bash
# Push Jackett/Prowlarr URL+API keys from .env into the running Library app.
#
# Admin Overview "configured" reads process Settings (env at container start).
# Writing .env alone is not enough — this force-recreates `app` and seeds
# app_settings so get_effective / Admin Config stay aligned.
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

is_placeholder() {
  local v="${1:-}"
  [[ -z "$v" || "$v" =~ ^[Yy]our- || "$v" == "changeme" ]]
}

JU="$(get_env JACKETT_URL)"
JU="${JU:-http://audiobook-jackett:9117}"
JK="$(get_env JACKETT_API_KEY)"
PU="$(get_env PROWLARR_URL)"
PU="${PU:-http://prowlarr:9696}"
PK="$(get_env PROWLARR_API_KEY)"

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
$DOCKER compose up -d --force-recreate --no-deps app

echo "Seeding config.jackett_* / config.prowlarr_* into app_settings ..."
ready=0
for _ in $(seq 1 30); do
  if $DOCKER compose exec -T app true >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "warn: app container not ready for exec — keys are in .env; recreate succeeded" >&2
  exit 0
fi

$DOCKER compose exec -T \
  -e JACKETT_URL_SEED="$JU" \
  -e JACKETT_API_KEY_SEED="$JK" \
  -e PROWLARR_URL_SEED="$PU" \
  -e PROWLARR_API_KEY_SEED="$PK" \
  app python - <<'PY'
import asyncio
import os

async def main() -> None:
    from app.services import app_settings
    from app.services.instance_settings import apply_runtime_overrides, invalidate_cache

    pairs = [
        ("config.jackett_url", os.environ.get("JACKETT_URL_SEED", "").strip()),
        ("config.jackett_api_key", os.environ.get("JACKETT_API_KEY_SEED", "").strip()),
        ("config.prowlarr_url", os.environ.get("PROWLARR_URL_SEED", "").strip()),
        ("config.prowlarr_api_key", os.environ.get("PROWLARR_API_KEY_SEED", "").strip()),
    ]
    for key, val in pairs:
        if val and not val.lower().startswith("your-"):
            await app_settings.set_setting(key, val)
            print(f"seeded {key}")
    invalidate_cache()
    await apply_runtime_overrides()
    print("apply_runtime_overrides done")

asyncio.run(main())
PY

echo "Jackett/Prowlarr keys applied (.env + app_settings + app recreate)"
