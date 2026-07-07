# Deploy Library Site to Pi via SSH
# Run from project root: .\deploy.ps1
# Builds frontend locally, ships migrations + Alembic config, rebuilds Docker,
# and ensures the nightly DB backup cron is installed on the Pi.

$ErrorActionPreference = "Stop"
$PI_USER = "pihole"
$PI_HOST = "pihole"
$REMOTE_PATH = "/opt/stacks/Library Site"

Write-Host "[1/8] Pre-deploy checks (pytest + tsc)..."
powershell -ExecutionPolicy Bypass -File "scripts\check.ps1" -SkipAndroid
if ($LASTEXITCODE -ne 0) { throw "Pre-deploy checks failed" }

Write-Host "[2/8] Building frontend locally..."
Push-Location frontend
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Frontend build failed" }
Pop-Location

Write-Host "[3/8] Creating deploy zip..."
$paths = @(
    "app",
    "backend",
    "migrations",
    "scripts",
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
    "alembic.ini",
    "tailscale-funnel-setup.sh",
    "docs"
)
Compress-Archive -Path $paths -DestinationPath "library-site-deploy.zip" -Force

Write-Host "[4/8] Uploading to Pi..."
scp library-site-deploy.zip "${PI_USER}@${PI_HOST}:/tmp/"

Write-Host "[5/8] Extracting on Pi..."
ssh "${PI_USER}@${PI_HOST}" "cd '$REMOTE_PATH' && unzip -o /tmp/library-site-deploy.zip"

Write-Host "[6/8] Ensuring nightly DB backup cron..."
ssh "${PI_USER}@${PI_HOST}" "cd '$REMOTE_PATH' && bash scripts/install_backup_cron.sh"

Write-Host "[7/8] Rebuilding Docker image..."
ssh "${PI_USER}@${PI_HOST}" "cd '$REMOTE_PATH' && docker compose build --no-cache app"

Write-Host "[8/8] Restarting app..."
ssh "${PI_USER}@${PI_HOST}" "cd '$REMOTE_PATH' && docker compose up -d app"

Write-Host "Deploy complete."
