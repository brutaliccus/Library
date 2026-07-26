# Install / bootstrap Library on Windows with Docker Desktop.
# Usage:
#   .\scripts\install_library.ps1
#   .\scripts\install_library.ps1 -Target "C:\dev\Library" -NonInteractive
#
# Prerequisites: Docker Desktop (Engine + Compose), Git (for clone),
# open ports 8085 / 9696 / 8191 / 9117.
param(
    [string]$Target = "",
    [string]$RepoUrl = "",
    [string]$Branch = "",
    [string]$AppUrl = "http://127.0.0.1:8085",
    [string]$SecretKey = "",
    [string]$AudioHost = "./media/audiobooks",
    [string]$EbookHost = "./media/ebooks",
    [string]$OlHost = "./media/openlibrary",
    [string]$ApkRepo = "brutaliccus/Library",
    [switch]$EnableVpn,
    [switch]$EnableDeepScrapers,
    [switch]$DisableLibraForgePipeline,
    [switch]$DisableEbookPipeline,
    [switch]$NonInteractive,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Write-Step([string]$Message) { Write-Host $Message -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host $Message -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host $Message -ForegroundColor Yellow }
function Write-Err([string]$Message) { Write-Host $Message -ForegroundColor Red }

function Read-Default([string]$Prompt, [string]$Default) {
    if ($NonInteractive) { return $Default }
    $suffix = if ($Default) { " [$Default]" } else { "" }
    $val = Read-Host "$Prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($val)) { return $Default }
    return $val
}

function Read-YesNo([string]$Prompt, [bool]$DefaultYes = $false) {
    if ($NonInteractive) { return $DefaultYes }
    $hint = if ($DefaultYes) { "Y/n" } else { "y/N" }
    $val = Read-Host "$Prompt [$hint]"
    if ([string]::IsNullOrWhiteSpace($val)) { return $DefaultYes }
    return $val -match '^[Yy]'
}

function Set-EnvKey([string]$EnvPath, [string]$Key, [string]$Value) {
    # Guard against accidental huge files (e.g. redirected logs).
    if (Test-Path $EnvPath) {
        $len = (Get-Item -LiteralPath $EnvPath).Length
        if ($len -gt 2MB) {
            throw "Refusing to edit .env - file is unexpectedly large ($len bytes): $EnvPath"
        }
    }

    # Always treat as a line array (PS5 Get-Content returns a scalar for 1-line files;
    # foreach on a scalar string iterates characters and can corrupt .env).
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path $EnvPath) {
        foreach ($line in [System.IO.File]::ReadAllLines($EnvPath)) {
            [void]$lines.Add($line)
        }
    }

    $found = $false
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match ("^" + [regex]::Escape($Key) + "=")) {
            $found = $true
            [void]$out.Add("$Key=$Value")
        }
        else {
            [void]$out.Add($line)
        }
    }
    if (-not $found) {
        [void]$out.Add("$Key=$Value")
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($EnvPath, $out.ToArray(), $utf8NoBom)
}

function New-SecretKey {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return ([System.BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Resolve-RepoRoot {
    if ($Target) { return $Target }
    if ($PSScriptRoot) {
        $candidate = Split-Path -Parent $PSScriptRoot
        if (Test-Path (Join-Path $candidate "docker-compose.yml")) {
            return $candidate
        }
    }
    return (Join-Path (Get-Location) "library")
}

function Ensure-Dir([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    $full = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $TARGET $Path }
    if (-not (Test-Path $full)) {
        Write-Warn "Creating $full"
        New-Item -ItemType Directory -Path $full -Force | Out-Null
    }
    return $full
}

Write-Step "==> Library installer (Windows / Docker Desktop)"
if (-not (Test-Command "docker")) {
    Write-Err "Docker is required. Install Docker Desktop, start it, then re-run."
    exit 1
}
docker compose version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Err "Docker Compose plugin required (docker compose)."
    exit 1
}
try {
    docker info 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker info failed" }
}
catch {
    Write-Err "Docker Engine is not running. Start Docker Desktop and wait until it is ready."
    exit 1
}

if (-not $RepoUrl) {
    $RepoUrl = if ($env:LIBRARY_SITE_REPO) { $env:LIBRARY_SITE_REPO } else { "https://github.com/brutaliccus/Library.git" }
}
if (-not $Branch) {
    $Branch = if ($env:LIBRARY_SITE_BRANCH) { $env:LIBRARY_SITE_BRANCH } else { "main" }
}

$TARGET = Resolve-RepoRoot
Write-Host "Target directory: $TARGET"

$hasGit = Test-Path (Join-Path $TARGET ".git")
$hasCompose = Test-Path (Join-Path $TARGET "docker-compose.yml")

if (-not $hasCompose) {
    if (-not (Test-Command "git")) {
        Write-Err "Git is required to clone the repository."
        exit 1
    }
    Write-Step "==> Cloning repository"
    $parent = Split-Path -Parent $TARGET
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if ((Test-Path $TARGET) -and -not (Get-ChildItem -Force $TARGET | Select-Object -First 1)) {
        Remove-Item -Path $TARGET -Force -Recurse -ErrorAction SilentlyContinue
    }
    if (Test-Path $TARGET) {
        Write-Warn "Directory exists - using existing tree (not re-cloning)."
    }
    else {
        git clone --branch $Branch $RepoUrl $TARGET
        if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
    }
}
elseif ($hasGit) {
    Write-Step "==> Updating existing checkout"
    Push-Location $TARGET
    try {
        git fetch --depth 1 origin $Branch 2>$null
        git checkout $Branch 2>$null
        git pull --ff-only 2>$null
    }
    catch {
        Write-Warn "git update skipped: $($_.Exception.Message)"
    }
    finally {
        Pop-Location
    }
}

Set-Location $TARGET

$envPath = Join-Path $TARGET ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $TARGET ".env.example") $envPath
    Write-Ok "Created .env from .env.example"
}
else {
    Write-Warn ".env already exists - will update selected keys only"
}

Write-Step "==> Core settings"
$APP_URL = Read-Default "Public site URL" $AppUrl
if (-not $SecretKey) { $SecretKey = New-SecretKey }
$SECRET_KEY = if ($NonInteractive) { $SecretKey } else { Read-Default "Secret key (random string)" $SecretKey }
Set-EnvKey $envPath "APP_URL" $APP_URL
Set-EnvKey $envPath "SECRET_KEY" $SECRET_KEY
Set-EnvKey $envPath "PUID" "1000"
Set-EnvKey $envPath "PGID" "1000"

Write-Step "==> Host media mounts (must exist)"
$AUDIO_HOST = Read-Default "Host audiobooks path" $AudioHost
$EBOOK_HOST = Read-Default "Host ebooks path" $EbookHost
$OL_HOST = Read-Default "Host Open Library dumps path (optional)" $OlHost

$audioFull = Ensure-Dir $AUDIO_HOST
$ebookFull = Ensure-Dir $EBOOK_HOST
Ensure-Dir $OL_HOST | Out-Null
$unorgAudio = Join-Path $audioFull ".unorganized"
$unorgEbook = Join-Path $ebookFull "unorganized"
New-Item -ItemType Directory -Path $unorgAudio -Force | Out-Null
New-Item -ItemType Directory -Path $unorgEbook -Force | Out-Null
$ignore = Join-Path $unorgAudio ".ignore"
if (-not (Test-Path $ignore)) { New-Item -ItemType File -Path $ignore -Force | Out-Null }

Set-EnvKey $envPath "AUDIOBOOK_HOST_DIR" $AUDIO_HOST
Set-EnvKey $envPath "EBOOK_HOST_DIR" $EBOOK_HOST
Set-EnvKey $envPath "OPENLIBRARY_HOST_DIR" $OL_HOST

if ($NonInteractive) {
    Write-Step "==> Skipping interactive integrations (configure in /admin/setup)"
    Set-EnvKey $envPath "ABS_URL" "http://host.docker.internal:13378"
    Set-EnvKey $envPath "KAVITA_URL" "http://host.docker.internal:5000"
}
else {
    Write-Step "==> Optional integrations (press Enter to skip)"
    $prowlarr = Read-Default "Prowlarr API key" ""
    $absUrl = Read-Default "Audiobookshelf URL" "http://host.docker.internal:13378"
    $absKey = Read-Default "Audiobookshelf API key" ""
    $absLib = Read-Default "Audiobookshelf library ID" ""
    $kavUrl = Read-Default "Kavita URL" "http://host.docker.internal:5000"
    $kavKey = Read-Default "Kavita API key" ""
    $rd = Read-Default "Real-Debrid API token (server default)" ""
    $tor = Read-Default "TorBox API token (optional second debrid)" ""
    if ($prowlarr) { Set-EnvKey $envPath "PROWLARR_API_KEY" $prowlarr }
    Set-EnvKey $envPath "ABS_URL" $absUrl
    if ($absKey) { Set-EnvKey $envPath "ABS_API_KEY" $absKey }
    if ($absLib) { Set-EnvKey $envPath "ABS_LIBRARY_ID" $absLib }
    Set-EnvKey $envPath "KAVITA_URL" $kavUrl
    if ($kavKey) { Set-EnvKey $envPath "KAVITA_API_KEY" $kavKey }
    if ($rd) { Set-EnvKey $envPath "REAL_DEBRID_API_TOKEN" $rd }
    if ($tor) { Set-EnvKey $envPath "TORBOX_API_TOKEN" $tor }
}

Write-Step "==> LibraForge / ebook pipelines"
Set-EnvKey $envPath "LIBRAFORGE_URL" "http://127.0.0.1:5056"
Set-EnvKey $envPath "LIBRAFORGE_INTERNAL_URL" "http://host.docker.internal:5056"
Set-EnvKey $envPath "LIBRAFORGE_M4B_JOBS" "1"
$lfOn = if ($NonInteractive) { -not $DisableLibraForgePipeline } else { Read-YesNo "Enable automated LibraForge audiobook pipeline?" $true }
$ebOn = if ($NonInteractive) { -not $DisableEbookPipeline } else { Read-YesNo "Enable ebook organizer pipeline?" $true }
Set-EnvKey $envPath "LIBRAFORGE_PIPELINE_ENABLED" ($(if ($lfOn) { "true" } else { "false" }))
Set-EnvKey $envPath "EBOOK_PIPELINE_ENABLED" ($(if ($ebOn) { "true" } else { "false" }))

$apk = Read-Default "GitHub owner/repo for Library APK releases" $ApkRepo
Set-EnvKey $envPath "ANDROID_APK_GITHUB_REPO" $apk

Write-Step "==> Scraper mode"
$deep = if ($NonInteractive) { [bool]$EnableDeepScrapers } else {
    Write-Warn "Deep FlareSolverr crawls are HIGH USAGE."
    Write-Host "Recommended: RSS-only (ABB + Knaben) - live Jackett search still works."
    Read-YesNo "Enable high-usage deep scrapers?" $false
}
if ($deep) {
    Set-EnvKey $envPath "ABB_RSS_ONLY" "false"
    Set-EnvKey $envPath "ABB_AUTHOR_CRAWL_ENABLED" "true"
    Set-EnvKey $envPath "SCRAPER_KNABEN_CRAWL_TASKS_PER_JOB" "8"
    Write-Warn "Deep scrapers enabled - monitor CPU."
}
else {
    Set-EnvKey $envPath "ABB_RSS_ONLY" "true"
    Set-EnvKey $envPath "ABB_AUTHOR_CRAWL_ENABLED" "false"
    Set-EnvKey $envPath "ABB_DEEP_SEARCH_ENABLED" "false"
    Set-EnvKey $envPath "ABB_LIVE_SEARCH_ENABLED" "false"
    Write-Ok "RSS-only defaults written to .env"
}

# VPN / gluetun is optional. Without WireGuard keys, leave profile off.
$vpn = [bool]$EnableVpn
if (-not $NonInteractive) {
    $vpn = Read-YesNo "Enable Mullvad VPN sidecar (gluetun) now? Requires WireGuard keys." $false
}
if ($vpn) {
    Set-EnvKey $envPath "COMPOSE_PROFILES" "vpn"
    Set-EnvKey $envPath "ABB_PROXY_URL" "http://gluetun:8888"
    Write-Warn "Set WIREGUARD_PRIVATE_KEY and WIREGUARD_ADDRESSES in .env, then: docker compose up -d"
}
else {
    Set-EnvKey $envPath "COMPOSE_PROFILES" ""
    Set-EnvKey $envPath "ABB_PROXY_URL" ""
    # Present empty keys so compose does not warn on unset WIREGUARD_* vars.
    Set-EnvKey $envPath "WIREGUARD_PRIVATE_KEY" ""
    Set-EnvKey $envPath "WIREGUARD_ADDRESSES" ""
    Write-Ok "VPN profile off - stack starts without gluetun. Configure Mullvad later in Admin."
}

foreach ($d in @("data", "prowlarr-config", "jackett-config", "media\audiobooks", "media\ebooks", "media\openlibrary")) {
    $p = Join-Path $TARGET $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArgs)
    # Docker writes progress/warnings to stderr; do not treat that as terminating.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & docker @ComposeArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            Write-Host $_.ToString()
        }
        else {
            Write-Host $_
        }
    }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return $code
}

Write-Step "==> Starting Docker stack"
Write-Warn "First boot imports the shipped indexer cache seed if the DB is empty. This may take a few minutes."
if ($SkipBuild) {
    $upCode = Invoke-Compose @("compose", "up", "-d")
}
else {
    $upCode = Invoke-Compose @("compose", "up", "-d", "--build")
}
if ($upCode -ne 0) {
    Write-Err "docker compose up failed - check: docker compose logs"
    exit 1
}

Write-Step "==> Waiting for app health"
$healthy = $false
for ($i = 1; $i -le 90; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8085/api/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) {
            Write-Ok "App is healthy"
            $healthy = $true
            break
        }
    }
    catch {
        # still starting
    }
    Start-Sleep -Seconds 2
}
if (-not $healthy) {
    Write-Warn "Health check timed out - check: docker compose logs app"
}

$syncPs1 = Join-Path $TARGET "scripts\sync_jackett_env.ps1"
if (Test-Path $syncPs1) {
    Write-Step "==> Syncing Jackett API key into .env"
    & powershell -ExecutionPolicy Bypass -File $syncPs1 -RepoRoot $TARGET
    [void](Invoke-Compose @("compose", "up", "-d", "app"))
}

Write-Ok ""
Write-Ok "Install complete."
Write-Host ""
Write-Host "Next steps:"
Write-Host ("  1. Open " + $APP_URL + "  or  http://127.0.0.1:8085")
Write-Host "  2. Create the admin account"
Write-Host "  3. Complete /admin/setup"
Write-Host "  4. ABS: confirm audiobook staging .unorganized is ignored"
Write-Host "  5. Kavita: exclude ebook staging folder unorganized"
Write-Host "  6. Optional Mullvad: WireGuard keys + COMPOSE_PROFILES=vpn + ABB_PROXY_URL=http://gluetun:8888"
Write-Host "  7. Optional LibraForge sibling - see docs/libraforge.md"
Write-Host ""
Write-Host ("Stack dir: " + $TARGET)
Write-Host ("Logs:      Set-Location '" + $TARGET + "'; docker compose logs -f app")
Write-Host "Ports:     app 8085 | prowlarr 9696 | flaresolverr 8191 | jackett 9117"
Write-Host ""
Write-Host "Note: Linux host cron helpers are skipped on Windows."
Write-Host "      Use Task Scheduler or Admin -> Catalog schedule instead."
