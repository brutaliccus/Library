#!/bin/bash
# Deploy Library Site on the Pi (extract zip, backup cron, rebuild Docker).
# Usually invoked by deploy.ps1 from Windows; can also be run directly on the Pi
# after manually placing library-site-deploy.zip in /tmp/.

set -e

PI_USER="pihole"
PI_HOST="pihole"
REMOTE_PATH="/opt/stacks/Library Site"

echo "[1/6] Creating deploy zip..."
cd "$(dirname "$0")"
rm -f library-site-deploy.zip
zip -r library-site-deploy.zip \
  app backend migrations scripts \
  Dockerfile docker-compose.yml requirements.txt alembic.ini \
  tailscale-funnel-setup.sh docs \
  -x "*.pyc" -x "*__pycache__*" -x "scripts/dev/*"

echo "[2/6] Uploading to Pi..."
scp library-site-deploy.zip "$PI_USER@$PI_HOST:/tmp/"

echo "[3/6] Extracting on Pi..."
ssh "$PI_USER@$PI_HOST" "cd '$REMOTE_PATH' && unzip -o /tmp/library-site-deploy.zip"

echo "[4/6] Ensuring nightly DB backup cron..."
ssh "$PI_USER@$PI_HOST" "cd '$REMOTE_PATH' && bash scripts/install_backup_cron.sh"

echo "[5/6] Rebuilding Docker image..."
ssh "$PI_USER@$PI_HOST" "cd '$REMOTE_PATH' && docker compose build --no-cache app"

echo "[6/6] Restarting app..."
ssh "$PI_USER@$PI_HOST" "cd '$REMOTE_PATH' && docker compose up -d app"

echo "Deploy complete."
