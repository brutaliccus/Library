#!/bin/bash
# Raise Jackett FlareSolverr timeout so ABB searches can complete on Pi.
set -euo pipefail
CFG="${1:-/opt/stacks/Library Site/jackett-config/Jackett/ServerConfig.json}"
if [ ! -f "$CFG" ]; then
  echo "Jackett config not found: $CFG"
  exit 0
fi
python3 - "$CFG" <<'PY'
import json
import sys
from pathlib import Path

cfg = Path(sys.argv[1])
data = json.loads(cfg.read_text())
old = data.get("FlareSolverrMaxTimeout", 0)
data["FlareSolverrMaxTimeout"] = 180000
if not data.get("FlareSolverrUrl"):
    data["FlareSolverrUrl"] = "http://audiobook-flaresolverr:8191"
cfg.write_text(json.dumps(data, indent=2) + "\n")
print(f"Jackett FlareSolverrMaxTimeout: {old} -> 180000")
PY
