#!/bin/bash
set -euo pipefail
ENV_FILE="/opt/stacks/Library Site/.env"
# Load only needed keys (avoid bash brace expansion issues in full .env)
while IFS= read -r line; do
  case "$line" in
    ABS_URL=*|ABS_API_KEY=*|ABS_LIBRARY_ID=*|LIBRAFORGE_INTERNAL_URL=*|LIBRAFORGE_URL=*)
      export "$line"
      ;;
  esac
done < "$ENV_FILE"
# Prefer docker-bridge / loopback; do not rely on public forge DNS for batch jobs
INTERNAL="${LIBRAFORGE_INTERNAL_URL:-}"
PUBLIC="${LIBRAFORGE_URL:-}"
if [ -n "$INTERNAL" ]; then
  export LIBRAFORGE_URL="$INTERNAL"
elif [ -n "$PUBLIC" ] && [[ "$PUBLIC" != *forge.library.freiverse.com* ]]; then
  export LIBRAFORGE_URL="$PUBLIC"
else
  export LIBRAFORGE_URL="http://127.0.0.1:5056"
fi
export LIBRARY_SITE_URL="${LIBRARY_SITE_URL:-http://127.0.0.1:8000}"
cd "/opt/stacks/Library Site"
exec python3 scripts/batch_chapter_forge.py \
  --root /mnt/Audiobooks \
  --docker-root /audiobooks \
  --concurrency 2 \
  --only-with-asin \
  --timeout 900 \
  --reports-dir /opt/stacks/libraforge/reports
