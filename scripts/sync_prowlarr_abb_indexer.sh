#!/bin/bash
# Non-fatal: Prowlarr indexer shape varies by version; deploy should continue.
set -uo pipefail
ROOT="${1:-/opt/stacks/Library Site}"
python3 "$ROOT/scripts/sync_prowlarr_abb_indexer.py" "$ROOT" || true
