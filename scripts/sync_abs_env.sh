#!/usr/bin/env bash
# Bootstrap Audiobookshelf (bundled-media) and copy API key / library id into .env.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"
BASE_URL="${ABS_BOOTSTRAP_URL:-http://127.0.0.1:13378}"
INTERNAL_URL="${ABS_INTERNAL_URL:-http://audiobookshelf:80}"
WAIT_SECONDS="${ABS_WAIT_SECONDS:-180}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "skip abs env (no .env)"
  exit 0
fi

set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    local esc
    esc=$(printf '%s' "$value" | sed -e 's/[&|\\]/\\&/g')
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    else
      sed -i '' "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    fi
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

get_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

echo "Waiting for Audiobookshelf at ${BASE_URL} ..."
ready=0
for _ in $(seq 1 $((WAIT_SECONDS / 2))); do
  if curl -fsS "${BASE_URL}/healthcheck" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "skip abs env (healthcheck timeout)"
  exit 0
fi

STATUS_JSON="$(curl -fsS "${BASE_URL}/status" 2>/dev/null || true)"
if [[ -z "$STATUS_JSON" ]]; then
  echo "skip abs env (status unreachable)"
  exit 0
fi

USER_NAME="$(get_env BUNDLED_ABS_USERNAME)"
USER_NAME="${USER_NAME:-admin}"
PASS="$(get_env BUNDLED_ABS_PASSWORD)"

IS_INIT="$(printf '%s' "$STATUS_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("isInit", False))' 2>/dev/null \
  || printf '%s' "$STATUS_JSON" | python -c 'import sys,json; print(json.load(sys.stdin).get("isInit", False))' 2>/dev/null \
  || echo "True")"

if [[ "$IS_INIT" == "False" || "$IS_INIT" == "false" ]]; then
  if [[ -z "$PASS" ]]; then
    PASS="$(openssl rand -hex 18 2>/dev/null || head -c 18 /dev/urandom | xxd -p -c 36)"
  fi
  if ! curl -fsS -X POST "${BASE_URL}/init" \
    -H 'Content-Type: application/json' \
    -d "{\"newRoot\":{\"username\":\"${USER_NAME}\",\"password\":\"${PASS}\"}}" >/dev/null; then
    echo "skip abs env (init failed)"
    exit 0
  fi
  set_env BUNDLED_ABS_USERNAME "$USER_NAME"
  set_env BUNDLED_ABS_PASSWORD "$PASS"
  echo "ABS root user initialized (${USER_NAME})"
elif [[ -z "$PASS" ]]; then
  echo "skip abs env (already initialized; set BUNDLED_ABS_PASSWORD or ABS_API_KEY manually)"
  exit 0
fi

LOGIN_JSON="$(curl -fsS -X POST "${BASE_URL}/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${USER_NAME}\",\"password\":\"${PASS}\"}" 2>/dev/null || true)"
TOKEN="$(printf '%s' "$LOGIN_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("user",{}).get("token",""))' 2>/dev/null \
  || printf '%s' "$LOGIN_JSON" | python -c 'import sys,json; print(json.load(sys.stdin).get("user",{}).get("token",""))' 2>/dev/null \
  || true)"
if [[ -z "${TOKEN:-}" ]]; then
  echo "skip abs env (empty token)"
  exit 0
fi

LIBS_JSON="$(curl -fsS "${BASE_URL}/api/libraries" -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)"
LIBRARY_ID="$(
  printf '%s' "$LIBS_JSON" | python3 -c '
import json,sys
data=json.load(sys.stdin)
libs=data.get("libraries") or []
for lib in libs:
    folders=[(f or {}).get("fullPath") for f in (lib.get("folders") or [])]
    if "/audiobooks" in folders:
        print(lib.get("id","") or ""); raise SystemExit
for lib in libs:
    if lib.get("mediaType")=="book":
        print(lib.get("id","") or ""); raise SystemExit
print("")
' 2>/dev/null || true
)"
if [[ -z "${LIBRARY_ID:-}" ]]; then
  CREATED="$(curl -fsS -X POST "${BASE_URL}/api/libraries" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{"name":"Audiobooks","folders":[{"fullPath":"/audiobooks"}],"mediaType":"book","provider":"audible","icon":"audiobookshelf"}' 2>/dev/null || true)"
  LIBRARY_ID="$(printf '%s' "$CREATED" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("id") or (d.get("library") or {}).get("id","") or "")' 2>/dev/null || true)"
  echo "ABS library created (/audiobooks)"
fi

set_env ABS_URL "$INTERNAL_URL"
set_env ABS_API_KEY "$TOKEN"
[[ -n "${LIBRARY_ID:-}" ]] && set_env ABS_LIBRARY_ID "$LIBRARY_ID"
echo "ABS_URL / ABS_API_KEY configured (internal ${INTERNAL_URL})"
