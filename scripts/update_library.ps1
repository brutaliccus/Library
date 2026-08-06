# Update an existing Library install from origin/main and rebuild the app.
#
# Run on the host from the install root:
#   cd C:\dev\Library
#   .\scripts\update_library.ps1
#   .\scripts\update_library.ps1 -Force
#
# Same semantics as update_library.sh: refuses dirty trees unless -Force,
# preserves .env / media / data, honors COMPOSE_PROFILES.

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipBuild,
    [switch]$SkipKeys,
    [string]$Branch = "main",
    [string]$Remote = "origin"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Write-Ok($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host $msg -ForegroundColor Red }

if (-not (Test-Path "docker-compose.yml") -and -not (Test-Path "compose.yml")) {
    Write-Err "error: no docker-compose.yml in $Root"
    exit 1
}
if (-not (Test-Path ".git")) {
    Write-Err "error: $Root is not a git checkout"
    exit 1
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Err "error: docker not found on PATH"
    exit 1
}

Write-Host "Library update"
Write-Host "  root:   $Root"
Write-Host "  remote: $Remote/$Branch"
Write-Host ""

$dirty = git status --porcelain --untracked-files=no
if ($dirty -and -not $Force) {
    Write-Err "Refusing to update: working tree has local modifications to tracked files."
    Write-Host ""
    git status --short --untracked-files=no
    Write-Host ""
    Write-Warn "Commit/stash those changes, or re-run with -Force to discard them"
    Write-Warn "(git reset --hard $Remote/$Branch). .env, data/, and media mounts are not in git."
    exit 2
}

$before = (git rev-parse --short HEAD).Trim()

Write-Ok "[1/4] Fetching $Remote/$Branch ..."
$shallow = (git rev-parse --is-shallow-repository).Trim()
if ($shallow -eq "true") {
    git fetch --depth 1 $Remote $Branch
} else {
    git fetch $Remote $Branch
}

git rev-parse --verify --quiet "$Remote/$Branch" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Err "error: missing $Remote/$Branch after fetch"
    exit 1
}

if ($dirty -and $Force) {
    Write-Warn "Discarding local tracked changes (-Force) ..."
}

git checkout -q $Branch 2>$null
if ($LASTEXITCODE -ne 0) {
    git checkout -q -B $Branch "$Remote/$Branch"
}
git reset --hard "$Remote/$Branch"

$after = (git rev-parse --short HEAD).Trim()
$afterMsg = (git log -1 --pretty=format:'%s').Trim()
Write-Ok "  HEAD: $before → $after  ($afterMsg)"

if ($SkipBuild) {
    Write-Warn "[2/4] Skipping docker build (-SkipBuild)"
    Write-Warn "[3/4] Skipping docker up (-SkipBuild)"
} else {
    Write-Ok "[2/4] Building app image ..."
    docker compose build app
    Write-Ok "[3/4] Recreating containers (honors COMPOSE_PROFILES from .env) ..."
    docker compose up -d
}

$applyKeys = Join-Path $Root "scripts\apply_indexer_keys.ps1"
$applyKeysSh = Join-Path $Root "scripts\apply_indexer_keys.sh"
if ($SkipKeys) {
    Write-Warn "[4/4] Skipping indexer key apply (-SkipKeys)"
} elseif (Test-Path $applyKeys) {
    Write-Ok "[4/4] Re-applying Jackett/Prowlarr keys (idempotent) ..."
    try {
        & $applyKeys
    } catch {
        Write-Warn "apply_indexer_keys.ps1 reported a problem — check Admin Overview."
    }
} elseif (Test-Path $applyKeysSh) {
    Write-Warn "[4/4] Found apply_indexer_keys.sh only — run under Git Bash/WSL if keys need reseeding."
} else {
    Write-Warn "[4/4] No apply_indexer_keys script — skipped"
}

Write-Host ""
Write-Ok "Update complete."
Write-Host "  commit:  $after"
Write-Host "  message: $afterMsg"
Write-Host ""
Write-Host "Health:"
docker compose ps
Write-Host ""
Write-Host "Next: open Admin → Health, or: docker compose logs -f app"
