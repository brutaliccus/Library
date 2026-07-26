#!/usr/bin/env bash
# Copy Jackett API key from ServerConfig.json into .env (repo-relative paths).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${JACKETT_CONFIG:-$ROOT/jackett-config/Jackett/ServerConfig.json}"
ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"

if [[ ! -f "$CFG" ]]; then
  echo "skip jackett env (no config yet)"
  exit 0
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "skip jackett env (no .env)"
  exit 0
fi

KEY="$(
  JACKETT_CFG_PATH="$CFG" python3 - <<'PY' 2>/dev/null \
    || JACKETT_CFG_PATH="$CFG" python - <<'PY' 2>/dev/null \
    || true
import json, os
print(json.load(open(os.environ["JACKETT_CFG_PATH"])).get("APIKey", "") or "")
PY
)"
if [[ -z "${KEY:-}" ]]; then
  echo "skip jackett env (empty API key)"
  exit 0
fi

if grep -qE '^JACKETT_API_KEY=' "$ENV_FILE"; then
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^JACKETT_API_KEY=.*|JACKETT_API_KEY=$KEY|" "$ENV_FILE"
  else
    sed -i '' "s|^JACKETT_API_KEY=.*|JACKETT_API_KEY=$KEY|" "$ENV_FILE"
  fi
else
  printf 'JACKETT_API_KEY=%s\n' "$KEY" >> "$ENV_FILE"
fi
echo "JACKETT_API_KEY configured"
if docker inspect audiobook-jackett >/dev/null 2>&1; then
  docker restart audiobook-jackett >/dev/null 2>&1 || true
fi
if [[ -x "$ROOT/scripts/wait_for_jackett.sh" ]]; then
  bash "$ROOT/scripts/wait_for_jackett.sh" || true
fi
