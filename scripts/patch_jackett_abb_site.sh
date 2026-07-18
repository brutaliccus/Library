#!/bin/bash
# Point Jackett ABB indexer at audiobookbay.is (often faster CF solve than .lu).
set -euo pipefail
CFG="${1:-/opt/stacks/Library Site/jackett-config/Jackett/Indexers/audiobookbay.json}"
if [ ! -f "$CFG" ]; then
  echo "Jackett ABB config not found: $CFG"
  exit 0
fi
python3 - "$CFG" <<'PY'
import json
import sys
from pathlib import Path

cfg = Path(sys.argv[1])
data = json.loads(cfg.read_text())
changed = False
for item in data:
    if item.get("id") == "sitelink":
        old = item.get("value")
        new = "http://audiobookbay.is/"
        if old != new:
            item["value"] = new
            changed = True
            print(f"Jackett ABB sitelink: {old} -> {new}")
if changed:
    cfg.write_text(json.dumps(data, indent=2) + "\n")
else:
    print("Jackett ABB sitelink already http://audiobookbay.is/")
PY
docker restart audiobook-jackett 2>/dev/null || true
