#!/bin/bash
# Check whether newer Open Library dumps are published (HEAD/etag/size only).
# Does NOT download or rebuild — admins are notified in-app; they click
# "Update catalog" in Admin → Config to start download + SQLite rebuild.
#
# Safe for daily cron. For a manual full rebuild use:
#   docker exec -e PYTHONPATH=/app "$CONTAINER" \
#     python /app/scripts/ol_import_dumps.py --force-download

set -euo pipefail

CONTAINER="${OL_CONTAINER:-audiobook-request}"

echo "[refresh-ol] $(date -Is) checking for newer Open Library dumps (no download)"
docker exec -e PYTHONPATH=/app "$CONTAINER" python -c "
import asyncio
from app.services.ol_catalog_build import check_for_updates

async def main():
    status = await check_for_updates(force=True, notify=True)
    print(
        'update_available=', status.get('new_dumps_available'),
        'changed=', status.get('changed_dumps'),
        'message=', (status.get('message') or '')[:160],
        sep='',
    )

asyncio.run(main())
"
echo "[refresh-ol] $(date -Is) check done"
