#!/usr/bin/env bash
# Bootstrap Kavita (bundled-media) and copy API key / library id into .env.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"
BASE_URL="${KAVITA_BOOTSTRAP_URL:-http://127.0.0.1:5000}"
INTERNAL_URL="${KAVITA_INTERNAL_URL:-http://kavita:5000}"
WAIT_SECONDS="${KAVITA_WAIT_SECONDS:-240}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "skip kavita env (no .env)"
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

echo "Waiting for Kavita at ${BASE_URL} ..."
ready=0
for _ in $(seq 1 $((WAIT_SECONDS / 3))); do
  if curl -fsS "${BASE_URL}/api/health" >/dev/null 2>&1 || curl -fsS -o /dev/null -w '' "${BASE_URL}/" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 3
done
if [[ "$ready" -ne 1 ]]; then
  echo "skip kavita env (health timeout)"
  exit 0
fi

USER_NAME="$(get_env BUNDLED_KAVITA_USERNAME)"
USER_NAME="${USER_NAME:-admin}"
PASS="$(get_env BUNDLED_KAVITA_PASSWORD)"
if [[ -z "$PASS" ]]; then
  PASS="$(openssl rand -hex 18 2>/dev/null || head -c 18 /dev/urandom | xxd -p -c 36)"
fi

if curl -fsS -X POST "${BASE_URL}/api/Account/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${USER_NAME}\",\"password\":\"${PASS}\",\"email\":\"\"}" >/dev/null 2>&1; then
  set_env BUNDLED_KAVITA_USERNAME "$USER_NAME"
  set_env BUNDLED_KAVITA_PASSWORD "$PASS"
  echo "Kavita admin registered (${USER_NAME})"
else
  STORED="$(get_env BUNDLED_KAVITA_PASSWORD)"
  if [[ -n "$STORED" ]]; then
    PASS="$STORED"
  else
    echo "skip kavita env (register skipped and no BUNDLED_KAVITA_PASSWORD)"
    exit 0
  fi
fi

LOGIN_JSON="$(curl -fsS -X POST "${BASE_URL}/api/Account/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${USER_NAME}\",\"password\":\"${PASS}\"}" 2>/dev/null || true)"
API_KEY="$(printf '%s' "$LOGIN_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("apiKey") or d.get("ApiKey") or "")' 2>/dev/null \
  || printf '%s' "$LOGIN_JSON" | python -c 'import sys,json; d=json.load(sys.stdin); print(d.get("apiKey") or d.get("ApiKey") or "")' 2>/dev/null \
  || true)"
JWT="$(printf '%s' "$LOGIN_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("token") or d.get("Token") or "")' 2>/dev/null \
  || printf '%s' "$LOGIN_JSON" | python -c 'import sys,json; d=json.load(sys.stdin); print(d.get("token") or d.get("Token") or "")' 2>/dev/null \
  || true)"
if [[ -z "${API_KEY:-}" ]]; then
  echo "skip kavita env (empty api key)"
  exit 0
fi

LIBS_JSON="$(curl -fsS "${BASE_URL}/api/Library/libraries" -H "x-api-key: ${API_KEY}" 2>/dev/null || true)"
LIBRARY_ID="$(
  printf '%s' "$LIBS_JSON" | python3 -c '
import json,sys
libs=json.load(sys.stdin) or []
if not isinstance(libs, list):
    libs=[]
for lib in libs:
    folders=lib.get("folders") or []
    if "/ebooks" in folders or lib.get("name")=="Ebooks":
        print(lib.get("id","") or ""); raise SystemExit
print("")
' 2>/dev/null || true
)"
if [[ -z "${LIBRARY_ID:-}" ]]; then
  AUTH_HEADER=(-H "x-api-key: ${API_KEY}")
  if [[ -n "${JWT:-}" ]]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${JWT}")
  fi
  CREATED="$(curl -fsS -X POST "${BASE_URL}/api/Library/create" \
    "${AUTH_HEADER[@]}" \
    -H 'Content-Type: application/json' \
    -d '{"name":"Ebooks","type":2,"folders":["/ebooks"],"folderWatching":true,"includeInDashboard":true,"includeInRecommended":true,"includeInSearch":true,"manageCollections":true,"manageReadingLists":true,"allowScrobbling":false,"excludePatterns":["**/unorganized/**","unorganized/**","**/unorganized","unorganized"],"fileGroupTypes":[1,2,3]}' 2>/dev/null || true)"
  LIBRARY_ID="$(printf '%s' "$CREATED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id","") or "")' 2>/dev/null || true)"
  echo "Kavita library created (/ebooks, excludes unorganized)"
fi

set_env KAVITA_URL "$INTERNAL_URL"
set_env KAVITA_API_KEY "$API_KEY"
[[ -n "${LIBRARY_ID:-}" ]] && set_env KAVITA_LIBRARY_ID "$LIBRARY_ID"
echo "KAVITA_URL / KAVITA_API_KEY configured (internal ${INTERNAL_URL})"
