#!/usr/bin/env bash
# Wire LibraForge URLs into .env for the bundled-media profile (no API key).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"
_lf_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
PUBLIC_URL="${LIBRAFORGE_BOOTSTRAP_URL:-http://${_lf_ip:-127.0.0.1}:5056}"
INTERNAL_URL="${LIBRAFORGE_INTERNAL_URL_DEFAULT:-http://libraforge:5056}"
WAIT_SECONDS="${LIBRAFORGE_WAIT_SECONDS:-180}"
# Health probe still hits localhost (published port on this host).
PROBE_URL="${LIBRAFORGE_PROBE_URL:-http://127.0.0.1:5056}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "skip libraforge env (no .env)"
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

echo "Waiting for LibraForge at ${PROBE_URL} (public URL ${PUBLIC_URL}) ..."
ready=0
for _ in $(seq 1 $((WAIT_SECONDS / 3))); do
  if curl -fsS "${PROBE_URL}/health" >/dev/null 2>&1 || curl -fsS -o /dev/null "${PROBE_URL}/" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 3
done
if [[ "$ready" -ne 1 ]]; then
  echo "skip libraforge env (health timeout)"
  exit 0
fi

set_env LIBRAFORGE_URL "$PUBLIC_URL"
set_env LIBRAFORGE_INTERNAL_URL "$INTERNAL_URL"
echo "LIBRAFORGE_URL / LIBRAFORGE_INTERNAL_URL configured"
