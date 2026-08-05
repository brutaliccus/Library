#!/usr/bin/env bash
# Download prebuilt Open Library catalog (data-seed release) into data/ol_catalog.db
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "python required for OL catalog fetch"
  exit 1
fi
exec "$PY" "$ROOT/scripts/fetch_ol_catalog.py" "$@"
