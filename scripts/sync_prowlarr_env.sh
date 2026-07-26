#!/usr/bin/env bash
# Copy Prowlarr API key from config.xml into .env (repo-relative).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${PROWLARR_CONFIG:-$ROOT/prowlarr-config/config.xml}"
ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"

if [[ ! -f "$CFG" ]]; then
  echo "skip prowlarr env (no config yet)"
  exit 0
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "skip prowlarr env (no .env)"
  exit 0
fi

KEY="$(
  PROWLARR_CFG_PATH="$CFG" python3 - <<'PY' 2>/dev/null \
    || PROWLARR_CFG_PATH="$CFG" python - <<'PY' 2>/dev/null \
    || true
import os, xml.etree.ElementTree as ET
root = ET.parse(os.environ["PROWLARR_CFG_PATH"]).getroot()
el = root.find("ApiKey")
print((el.text or "").strip() if el is not None else "")
PY
)"
if [[ -z "${KEY:-}" ]]; then
  echo "skip prowlarr env (empty API key)"
  exit 0
fi

if grep -qE '^PROWLARR_API_KEY=' "$ENV_FILE"; then
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^PROWLARR_API_KEY=.*|PROWLARR_API_KEY=$KEY|" "$ENV_FILE"
  else
    sed -i '' "s|^PROWLARR_API_KEY=.*|PROWLARR_API_KEY=$KEY|" "$ENV_FILE"
  fi
else
  printf 'PROWLARR_API_KEY=%s\n' "$KEY" >> "$ENV_FILE"
fi
echo "PROWLARR_API_KEY configured"
