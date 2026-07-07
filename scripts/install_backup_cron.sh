#!/bin/bash
# Idempotent install of the nightly app.db backup cron on the Pi host.
# Safe to run on every deploy — skips if the entry already exists.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKUP_SCRIPT="$SCRIPT_DIR/backup_db.sh"
LOG_FILE="$PROJECT_ROOT/data/backups/backup.log"
CRON_LINE="15 3 * * * \"$BACKUP_SCRIPT\" >> \"$LOG_FILE\" 2>&1"

if [ ! -f "$BACKUP_SCRIPT" ]; then
    echo "backup_db.sh not found at $BACKUP_SCRIPT" >&2
    exit 1
fi

chmod +x "$BACKUP_SCRIPT"
mkdir -p "$PROJECT_ROOT/data/backups"

if crontab -l 2>/dev/null | grep -Fq "backup_db.sh"; then
    echo "Backup cron already installed."
    exit 0
fi

(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo "Installed nightly backup cron (03:15 -> $LOG_FILE)."
