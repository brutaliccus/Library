#!/bin/bash
# Rebuild the local Open Library catalog from the latest monthly dumps.
# Runs the importer inside the app container so it uses the app config/paths.
# Raw dumps download to /openlibrary/dumps (HDD); the query DB is built on the
# SSD at /app/data/ol_catalog.db and atomically swapped in when complete.
#
# Intended to run monthly via cron (see install_ol_catalog_cron.sh). Safe to run
# manually any time; the live DB is only replaced once the build fully succeeds.

set -euo pipefail

CONTAINER="${OL_CONTAINER:-audiobook-request}"

echo "[refresh-ol] $(date -Is) starting Open Library catalog rebuild"
docker exec -e PYTHONPATH=/app "$CONTAINER" python /app/scripts/ol_import_dumps.py "$@"
echo "[refresh-ol] $(date -Is) done"
