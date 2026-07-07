#!/bin/bash
# Nightly SQLite backup for Library Site (app.db holds the torrent cache,
# users, progress, and admin settings).
#
# Uses SQLite's online backup so it is safe to run while the app is writing
# (a plain `cp` of a WAL-mode database can produce a corrupt copy).
#
# Install on the Pi (as the user that owns /opt/stacks):
#   chmod +x "/opt/stacks/Library Site/scripts/backup_db.sh"
#   crontab -e   # then add:
#   15 3 * * * "/opt/stacks/Library Site/scripts/backup_db.sh" >> "/opt/stacks/Library Site/data/backups/backup.log" 2>&1
#
# Optional overrides:
#   DB_PATH=/path/to/app.db BACKUP_DIR=/path/to/backups RETENTION_DAYS=30 ./backup_db.sh
#
# Cron is installed automatically by deploy.ps1 (via scripts/install_backup_cron.sh).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

DB_PATH="${DB_PATH:-$PROJECT_ROOT/data/app.db}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_ROOT/data/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

if [ ! -f "$DB_PATH" ]; then
    echo "[$(date -Is)] ERROR: database not found at $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/app-$STAMP.db"

if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_PATH" ".backup '$OUT'"
else
    # Pi OS ships python3 with the sqlite3 module even when the CLI isn't installed.
    python3 - "$DB_PATH" "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
fi

gzip -f "$OUT"
echo "[$(date -Is)] backup written: $OUT.gz ($(du -h "$OUT.gz" | cut -f1))"

# Prune backups older than the retention window.
find "$BACKUP_DIR" -name 'app-*.db.gz' -mtime "+$RETENTION_DAYS" -delete
