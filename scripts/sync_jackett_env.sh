#!/bin/bash
set -euo pipefail
CFG="/opt/stacks/Library Site/jackett-config/Jackett/ServerConfig.json"
ENV_FILE="/opt/stacks/Library Site/.env"
KEY=$(python3 -c "import json; print(json.load(open('$CFG')).get('APIKey',''))")
if [ -z "$KEY" ] || [ ! -f "$ENV_FILE" ]; then
  echo "skip jackett env"
  exit 0
fi
if grep -q '^JACKETT_API_KEY=' "$ENV_FILE"; then
  sed -i "s/^JACKETT_API_KEY=.*/JACKETT_API_KEY=$KEY/" "$ENV_FILE"
else
  echo "JACKETT_API_KEY=$KEY" >> "$ENV_FILE"
fi
echo "JACKETT_API_KEY configured"
docker restart audiobook-jackett
bash "$(dirname "$0")/wait_for_jackett.sh"
