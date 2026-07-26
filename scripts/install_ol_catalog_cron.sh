#!/bin/bash
# Idempotent install of a daily Open Library dump *check* cron on the Pi.
# Check-only: notifies admins when remote dumps change. Never auto-downloads.
# Safe to run on every deploy — replaces an older monthly full-refresh entry.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REFRESH_SCRIPT="$SCRIPT_DIR/refresh_ol_catalog.sh"
LOG_FILE="$PROJECT_ROOT/data/ol_catalog_refresh.log"
# Daily 05:15 — dumps publish monthly; daily HEAD is cheap.
CRON_LINE="15 5 * * * \"$REFRESH_SCRIPT\" >> \"$LOG_FILE\" 2>&1"

if [ ! -f "$REFRESH_SCRIPT" ]; then
    echo "refresh_ol_catalog.sh not found at $REFRESH_SCRIPT" >&2
    exit 1
fi

chmod +x "$REFRESH_SCRIPT"
mkdir -p "$PROJECT_ROOT/data"

EXISTING="$(crontab -l 2>/dev/null || true)"
# Drop any prior refresh_ol_catalog.sh lines (legacy monthly download or old check).
FILTERED="$(printf '%s\n' "$EXISTING" | grep -vF "refresh_ol_catalog.sh" || true)"
{ printf '%s\n' "$FILTERED"; echo "$CRON_LINE"; } | grep -v '^$' | crontab -
echo "Installed daily OL dump check cron (05:15 -> $LOG_FILE). Notify only; no auto-download."
