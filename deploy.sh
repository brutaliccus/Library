#!/bin/bash
# Deploy Library Site to Pi via SSH
# Run from Windows (Git Bash or WSL) or from project root on a machine with ssh/scp

set -e

PI_USER="pihole"
PI_HOST="pihole"
REMOTE_PATH="/opt/stacks/Library Site"

echo "[1/5] Creating deploy zip..."
cd "$(dirname "$0")"
rm -f library-site-deploy.zip
zip -r library-site-deploy.zip app backend Dockerfile docker-compose.yml requirements.txt \
  tailscale-funnel-setup.sh docs \
  frontend/src frontend/package.json frontend/package-lock.json frontend/vite.config.ts \
  frontend/tsconfig.json frontend/index.html frontend/tailwind.config.js frontend/postcss.config.js \
  -x "*.pyc" -x "*__pycache__*" -x "frontend/node_modules/*"

echo "[2/5] Uploading to Pi..."
scp library-site-deploy.zip "$PI_USER@$PI_HOST:/tmp/"

echo "[3/5] Extracting and building frontend on Pi..."
ssh "$PI_USER@$PI_HOST" "cd '$REMOTE_PATH' && unzip -o /tmp/library-site-deploy.zip && cd frontend && rm -rf node_modules && npm install && npm run build"

echo "[4/5] Rebuilding Docker image..."
ssh "$PI_USER@$PI_HOST" "cd '$REMOTE_PATH' && docker compose build --no-cache app"

echo "[5/5] Restarting app..."
ssh "$PI_USER@$PI_HOST" "cd '$REMOTE_PATH' && docker compose up -d app"

echo "Deploy complete."
