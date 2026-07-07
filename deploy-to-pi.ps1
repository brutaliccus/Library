# Deploy Library Site to Pi
# Run from: c:\dev\Library Site
# Requires: WSL (for rsync), SSH access to pi@192.168.68.76

$ErrorActionPreference = "Stop"
$PI_HOST = "pihole@pihole"
$PI_PATH = "/opt/stacks/Library Site"
# Convert Windows path to WSL path (e.g. C:\dev\Library Site -> /mnt/c/dev/Library Site)
$SRC = (Resolve-Path .).Path
$SRC_WSL = "/mnt/" + $SRC.Substring(0,1).ToLower() + ($SRC.Substring(2) -replace '\\', '/')

Write-Host "=== [1/4] Syncing files to Pi ===" -ForegroundColor Cyan
$env:RSYNC_SRC = $SRC_WSL
$env:RSYNC_DST = "${PI_HOST}:${PI_PATH}/"
wsl -e bash -c 'rsync -avz --exclude node_modules --exclude __pycache__ --exclude .venv --exclude venv --exclude data --exclude .git -e ssh "$RSYNC_SRC/" "$RSYNC_DST"'

Write-Host "`n=== [2/4] Running deploy on Pi ===" -ForegroundColor Cyan
ssh -t $PI_HOST "cd $PI_PATH && chmod +x deploy.sh && ./deploy.sh"

Write-Host "`nDeploy complete." -ForegroundColor Green
