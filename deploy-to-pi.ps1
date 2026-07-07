# Deploy Library Site to Pi via rsync (alternative to deploy.ps1)
# Run from: c:\dev\Library Site
# Requires: WSL (for rsync), SSH access to the Pi
#
# deploy.ps1 is the usual path (local frontend build + zip). Use this script
# when you want to sync the full tree and build the frontend on the Pi instead.

$ErrorActionPreference = "Stop"
$PI_HOST = "pihole@pihole"
$PI_PATH = "/opt/stacks/Library Site"
$SRC = (Resolve-Path .).Path
$SRC_WSL = "/mnt/" + $SRC.Substring(0,1).ToLower() + ($SRC.Substring(2) -replace '\\', '/')

Write-Host "=== [1/5] Pre-deploy checks (pytest + tsc) ===" -ForegroundColor Cyan
powershell -ExecutionPolicy Bypass -File "scripts\check.ps1" -SkipAndroid
if ($LASTEXITCODE -ne 0) { throw "Pre-deploy checks failed" }

Write-Host "`n=== [2/5] Syncing files to Pi ===" -ForegroundColor Cyan
$env:RSYNC_SRC = $SRC_WSL
$env:RSYNC_DST = "${PI_HOST}:${PI_PATH}/"
wsl -e bash -c 'rsync -avz --exclude node_modules --exclude __pycache__ --exclude .venv --exclude venv --exclude data --exclude .git -e ssh "$RSYNC_SRC/" "$RSYNC_DST"'

Write-Host "`n=== [3/5] Building frontend on Pi ===" -ForegroundColor Cyan
ssh -t $PI_HOST "cd '$PI_PATH/frontend' && npm install && npm run build"

Write-Host "`n=== [4/5] Ensuring nightly DB backup cron ===" -ForegroundColor Cyan
ssh -t $PI_HOST "cd '$PI_PATH' && bash scripts/install_backup_cron.sh"

Write-Host "`n=== [5/5] Rebuilding and restarting app ===" -ForegroundColor Cyan
ssh -t $PI_HOST "cd '$PI_PATH' && docker compose build --no-cache app && docker compose up -d app"

Write-Host "`nDeploy complete." -ForegroundColor Green
