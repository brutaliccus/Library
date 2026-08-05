#!/usr/bin/env bash
# Thin wrapper around configure_prowlarr.py
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export LIBRARY_ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "skip prowlarr configure (python required)"
  exit 0
fi
exec "$PY" "$ROOT/scripts/configure_prowlarr.py" "$@"
