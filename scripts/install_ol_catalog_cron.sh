#!/bin/bash
# Idempotent install of the monthly Open Library catalog refresh cron on the Pi.
# Safe to run on every deploy — skips if the entry already exists.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REFRESH_SCRIPT="$SCRIPT_DIR/refresh_ol_catalog.sh"
LOG_FILE="$PROJECT_ROOT/data/ol_catalog_refresh.log"
# 04:30 on the 5th of each month (dumps for the prior month are published by then).
CRON_LINE="30 4 5 * * \"$REFRESH_SCRIPT\" >> \"$LOG_FILE\" 2>&1"

if [ ! -f "$REFRESH_SCRIPT" ]; then
    echo "refresh_ol_catalog.sh not found at $REFRESH_SCRIPT" >&2
    exit 1
fi

chmod +x "$REFRESH_SCRIPT"
mkdir -p "$PROJECT_ROOT/data"

if crontab -l 2>/dev/null | grep -Fq "refresh_ol_catalog.sh"; then
    echo "OL catalog refresh cron already installed."
    exit 0
fi

(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo "Installed monthly OL catalog refresh cron (05th 04:30 -> $LOG_FILE)."
