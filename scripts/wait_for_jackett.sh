#!/bin/bash
# Wait until Jackett responds on the host-mapped port (after container restart).
set -uo pipefail
URL="${JACKETT_WAIT_URL:-http://127.0.0.1:9117/UI/Dashboard}"
MAX_ATTEMPTS="${JACKETT_WAIT_ATTEMPTS:-45}"
SLEEP_SECS="${JACKETT_WAIT_SLEEP:-2}"

for ((i = 1; i <= MAX_ATTEMPTS; i++)); do
  if curl -sf -o /dev/null --max-time 3 "$URL" 2>/dev/null; then
    echo "Jackett ready (${i}/${MAX_ATTEMPTS})"
    exit 0
  fi
  sleep "$SLEEP_SECS"
done

echo "Jackett not ready after $((MAX_ATTEMPTS * SLEEP_SECS))s — continuing anyway" >&2
exit 0
