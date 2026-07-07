# Deploy Library Site to Pi via SSH
# Run from project root: .\deploy.ps1
# Builds frontend locally so we deploy exactly what we test (no Pi build ambiguity)

$ErrorActionPreference = "Stop"
$PI_USER = "pihole"
$PI_HOST = "pihole"
$REMOTE_PATH = "/opt/stacks/Library Site"

Write-Host "[1/6] Building frontend locally..."
Push-Location frontend
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Frontend build failed" }
Pop-Location

Write-Host "[2/6] Creating deploy zip..."
$paths = @(
    "app", "backend", "Dockerfile", "docker-compose.yml", "requirements.txt",
    "tailscale-funnel-setup.sh", "docs"
)
Compress-Archive -Path $paths -DestinationPath "library-site-deploy.zip" -Force

Write-Host "[3/6] Uploading to Pi..."
scp library-site-deploy.zip "${PI_USER}@${PI_HOST}:/tmp/"

Write-Host "[4/6] Extracting on Pi..."
ssh "${PI_USER}@${PI_HOST}" "cd '$REMOTE_PATH' && unzip -o /tmp/library-site-deploy.zip"

Write-Host "[5/6] Rebuilding Docker image..."
ssh "${PI_USER}@${PI_HOST}" "cd '$REMOTE_PATH' && docker compose build --no-cache app"

Write-Host "[6/6] Restarting app..."
ssh "${PI_USER}@${PI_HOST}" "cd '$REMOTE_PATH' && docker compose up -d app"

Write-Host "Deploy complete."
