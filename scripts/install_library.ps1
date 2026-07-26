# Install / bootstrap Library on Windows with Docker Desktop.
# Usage:
#   .\scripts\install_library.ps1
#   .\scripts\install_library.ps1 -Target "C:\dev\Library" -NonInteractive
#
# Prerequisites: Docker Desktop (Engine + Compose), Git (for clone),
# open ports 8085 / 9696 / 8191 / 9117 / 13378 / 5000 / 5056 (bundled-media).
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
    [string]$LibraForgeRepo = "https://github.com/coconautilus17/LibraForge.git",
    [switch]$EnableVpn,
    [switch]$EnableDeepScrapers,
    [switch]$DisableLibraForgePipeline,
    [switch]$DisableEbookPipeline,
    [switch]$SkipBundledMedia,
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

function Test-SeedPresent([string]$SeedGz) {
    return (Test-Path $SeedGz) -and ((Get-Item -LiteralPath $SeedGz).Length -gt 1MB)
}

function Get-EnvKeyValue([string]$EnvPath, [string]$Key) {
    if (-not (Test-Path $EnvPath)) { return "" }
    foreach ($line in [System.IO.File]::ReadAllLines($EnvPath)) {
        if ($line -match ("^" + [regex]::Escape($Key) + "=(.*)$")) {
            return $Matches[1].Trim()
        }
    }
    return ""
}

function Merge-ComposeProfiles([string[]]$Profiles) {
    $parts = @()
    foreach ($p in $Profiles) {
        if ([string]::IsNullOrWhiteSpace($p)) { continue }
        foreach ($piece in ($p -split ',')) {
            $t = $piece.Trim()
            if ($t -and ($parts -notcontains $t)) { $parts += $t }
        }
    }
    return ($parts -join ",")
}

function Test-LooksExternalMediaUrl([string]$Url) {
    if ([string]::IsNullOrWhiteSpace($Url)) { return $false }
    if ($Url -match 'your-|placeholder|changeme') { return $false }
    if ($Url -match 'audiobookshelf|://kavita(:|/|$)|libraforge|host\.docker\.internal|127\.0\.0\.1|localhost') {
        return $false
    }
    return $true
}

function Ensure-LibraForgeClone([string]$RepoRoot, [string]$GitUrl) {
    $lfDir = Join-Path $RepoRoot "libraforge"
    $dockerfile = Join-Path $lfDir "Dockerfile"
    if (Test-Path $dockerfile) {
        Write-Ok "LibraForge companion present at $lfDir"
        return $true
    }
    if (-not (Test-Command "git")) {
        Write-Warn "Git required to clone LibraForge companion - bundled LibraForge skipped"
        return $false
    }
    Write-Step "==> Cloning LibraForge companion into ./libraforge"
    if ((Test-Path $lfDir) -and -not (Get-ChildItem -Force $lfDir | Select-Object -First 1)) {
        Remove-Item -Path $lfDir -Force -Recurse -ErrorAction SilentlyContinue
    }
    if (Test-Path $lfDir) {
        Write-Warn "libraforge/ exists but has no Dockerfile - not overwriting"
        return $false
    }
    git clone --depth 1 $GitUrl $lfDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dockerfile)) {
        Write-Warn "LibraForge clone failed - continue without bundled LibraForge service"
        return $false
    }
    Write-Ok "LibraForge cloned"
    return $true
}

function Ensure-IndexerSeed([string]$RepoRoot) {
    # Warm torrent/indexer cache (~36 MB gzip -> ~150 MB on first-boot import).
    # Prefer repo/LFS copy; else download the GitHub Release asset (optional if it fails).
    $seedDir = Join-Path $RepoRoot "seed"
    $seedGz = Join-Path $seedDir "indexer_cache.db.gz"
    if (Test-SeedPresent $seedGz) {
        $mb = [math]::Round((Get-Item -LiteralPath $seedGz).Length / 1MB, 1)
        Write-Ok "Indexer cache seed present $($mb) MB compressed"
        return
    }

    if ((Test-Path (Join-Path $RepoRoot ".gitattributes")) -and (Test-Command "git")) {
        Write-Step "==> Pulling indexer cache seed via Git LFS (if tracked)"
        Push-Location $RepoRoot
        try {
            git lfs pull --include "seed/indexer_cache.db.gz" 2>$null | Out-Null
        }
        catch {
            Write-Warn "git lfs pull skipped: $($_.Exception.Message)"
        }
        finally {
            Pop-Location
        }
        if (Test-SeedPresent $seedGz) {
            Write-Ok "Indexer cache seed restored via Git LFS"
            return
        }
    }

    if (-not (Test-Path $seedDir)) {
        New-Item -ItemType Directory -Path $seedDir -Force | Out-Null
    }

    $urls = @(
        "https://github.com/brutaliccus/Library/releases/download/data-seed/indexer_cache.db.gz",
        "https://github.com/brutaliccus/Library/releases/download/data-seed/seed-cache",
        "https://github.com/brutaliccus/Library/releases/latest/download/indexer_cache.db.gz"
    )
    foreach ($url in $urls) {
        Write-Warn "Downloading indexer cache seed from $url ..."
        try {
            Invoke-WebRequest -Uri $url -OutFile $seedGz -UseBasicParsing
            if (Test-SeedPresent $seedGz) {
                Write-Ok "Downloaded indexer cache seed"
                return
            }
        }
        catch {
            Write-Warn "Download failed: $($_.Exception.Message)"
        }
    }
    Write-Warn "Indexer cache seed missing - install continues; first boot starts with an empty cache (optional)."
    Write-Warn "Place seed/indexer_cache.db.gz manually or re-run after the data-seed GitHub Release is available."
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

# Bundled ABS+Kavita+LibraForge on the compose network (default for fresh installs).
# Skip when operator opts out, or when .env already points at an external media stack.
$existingAbsUrl = Get-EnvKeyValue $envPath "ABS_URL"
$existingAbsKey = Get-EnvKeyValue $envPath "ABS_API_KEY"
$externalAlready = (Test-LooksExternalMediaUrl $existingAbsUrl) -or (
    $existingAbsKey -and $existingAbsKey -notmatch 'your-|placeholder'
)
$bundledDefault = -not $SkipBundledMedia -and -not $externalAlready
$useBundled = if ($NonInteractive) {
    [bool]$bundledDefault
}
else {
    if ($externalAlready) {
        Write-Warn "Existing external ABS/Kavita settings detected - bundled media off by default."
        Read-YesNo "Start bundled Audiobookshelf + Kavita + LibraForge (Docker profile bundled-media)?" $false
    }
    else {
        Write-Step "==> Bundled media stack (recommended for new installs)"
        Write-Host "Starts Audiobookshelf (:13378), Kavita (:5000), and LibraForge (:5056) on the same Docker network."
        Write-Host "API keys are bootstrapped into .env after first start - no manual key entry."
        Write-Warn "Adds ~1-2 GB RAM vs core indexer stack alone."
        Read-YesNo "Enable bundled media stack (profile bundled-media)?" $true
    }
}

if ($useBundled) {
    if (-not (Ensure-LibraForgeClone $TARGET $LibraForgeRepo)) {
        Write-Warn "LibraForge companion unavailable - bundled-media disabled for this run"
        $useBundled = $false
    }
}
if ($useBundled) {
    Set-EnvKey $envPath "ABS_URL" "http://audiobookshelf:80"
    Set-EnvKey $envPath "KAVITA_URL" "http://kavita:5000"
    Set-EnvKey $envPath "LIBRAFORGE_URL" "http://127.0.0.1:5056"
    Set-EnvKey $envPath "LIBRAFORGE_INTERNAL_URL" "http://libraforge:5056"
    Write-Ok "Bundled-media URLs written (keys sync after containers start)"
}
elseif (-not $NonInteractive) {
    Write-Step "==> External stack integrations (Enter keeps defaults; keys can wait for /admin/setup)"
    $defaultAbsUrl = if ($existingAbsUrl) { $existingAbsUrl } else { "http://host.docker.internal:13378" }
    $defaultKavUrl = Get-EnvKeyValue $envPath "KAVITA_URL"
    if (-not $defaultKavUrl) { $defaultKavUrl = "http://host.docker.internal:5000" }
    $prowlarr = Read-Default "Prowlarr API key" ""
    $absUrl = Read-Default "Audiobookshelf URL" $defaultAbsUrl
    $absKey = Read-Default "Audiobookshelf API key" ""
    $absLib = Read-Default "Audiobookshelf library ID" ""
    $kavUrl = Read-Default "Kavita URL" $defaultKavUrl
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
    else { Set-EnvKey $envPath "REAL_DEBRID_API_TOKEN" "" }
    if ($tor) { Set-EnvKey $envPath "TORBOX_API_TOKEN" $tor }
}
elseif (-not $externalAlready) {
    # NonInteractive + skipped bundled: leave placeholders for /admin/setup.
    Set-EnvKey $envPath "ABS_URL" "http://host.docker.internal:13378"
    Set-EnvKey $envPath "KAVITA_URL" "http://host.docker.internal:5000"
}

Write-Step "==> LibraForge / ebook pipelines"
$lfOn = if ($NonInteractive) { -not $DisableLibraForgePipeline } else { Read-YesNo "Enable automated LibraForge audiobook pipeline?" $true }
$ebOn = if ($NonInteractive) { -not $DisableEbookPipeline } else { Read-YesNo "Enable ebook organizer pipeline?" $true }
if (-not $useBundled) {
    if (-not (Get-EnvKeyValue $envPath "LIBRAFORGE_URL")) {
        Set-EnvKey $envPath "LIBRAFORGE_URL" "http://127.0.0.1:5056"
    }
    if (-not (Get-EnvKeyValue $envPath "LIBRAFORGE_INTERNAL_URL")) {
        Set-EnvKey $envPath "LIBRAFORGE_INTERNAL_URL" "http://host.docker.internal:5056"
    }
}
Set-EnvKey $envPath "LIBRAFORGE_M4B_JOBS" "1"
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

# VPN / gluetun is optional and OFF by default on Windows (Mullvad not required).
$vpn = [bool]$EnableVpn
if (-not $NonInteractive) {
    $vpn = Read-YesNo "Enable Mullvad VPN sidecar (gluetun) now? Optional - not required. Needs WireGuard keys." $false
}
$profileParts = @()
if ($useBundled) { $profileParts += "bundled-media" }
if ($vpn) {
    $profileParts += "vpn"
    Set-EnvKey $envPath "ABB_PROXY_URL" "http://gluetun:8888"
    Write-Warn "Set WIREGUARD_PRIVATE_KEY and WIREGUARD_ADDRESSES in .env if not already present."
}
else {
    Set-EnvKey $envPath "ABB_PROXY_URL" ""
    # Present empty keys so compose does not warn on unset WIREGUARD_* vars.
    Set-EnvKey $envPath "WIREGUARD_PRIVATE_KEY" ""
    Set-EnvKey $envPath "WIREGUARD_ADDRESSES" ""
    Write-Ok "VPN profile off - stack starts without gluetun. Configure Mullvad later in Admin."
}
$mergedProfiles = Merge-ComposeProfiles $profileParts
Set-EnvKey $envPath "COMPOSE_PROFILES" $mergedProfiles
if ($useBundled) {
    Write-Ok "COMPOSE_PROFILES=$mergedProfiles (bundled-media starts ABS/Kavita/LibraForge)"
}

foreach ($d in @(
        "data", "prowlarr-config", "jackett-config",
        "audiobookshelf-config", "audiobookshelf-metadata", "kavita-config",
        "libraforge-auth", "libraforge-config", "libraforge-reports",
        "media\audiobooks", "media\ebooks", "media\openlibrary"
    )) {
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

Write-Step "==> Ensuring indexer cache seed"
Ensure-IndexerSeed $TARGET

Write-Step "==> Starting Docker stack"
Write-Warn "First boot imports seed/indexer_cache.db.gz into an empty DB (~150 MB). This may take a few minutes."
if ($useBundled) {
    Write-Warn "First LibraForge image build can take several minutes."
    Write-Host "Bundled media keys sync automatically after services are healthy."
}
else {
    Write-Host "After create-admin / create-library / offline PIN, /admin/setup configures ABS, Kavita, and LibraForge."
}
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
}

$prowlSyncPs1 = Join-Path $TARGET "scripts\sync_prowlarr_env.ps1"
if (Test-Path $prowlSyncPs1) {
    Write-Step "==> Syncing Prowlarr API key into .env"
    & powershell -ExecutionPolicy Bypass -File $prowlSyncPs1 -RepoRoot $TARGET
}

if ($useBundled) {
    $absSync = Join-Path $TARGET "scripts\sync_abs_env.ps1"
    $kavSync = Join-Path $TARGET "scripts\sync_kavita_env.ps1"
    $lfSync = Join-Path $TARGET "scripts\sync_libraforge_env.ps1"
    if (Test-Path $absSync) {
        Write-Step "==> Bootstrapping Audiobookshelf API key + library"
        & powershell -ExecutionPolicy Bypass -File $absSync -RepoRoot $TARGET
    }
    if (Test-Path $kavSync) {
        Write-Step "==> Bootstrapping Kavita API key + library"
        & powershell -ExecutionPolicy Bypass -File $kavSync -RepoRoot $TARGET
    }
    if (Test-Path $lfSync) {
        Write-Step "==> Wiring LibraForge URLs"
        & powershell -ExecutionPolicy Bypass -File $lfSync -RepoRoot $TARGET
    }
}

[void](Invoke-Compose @("compose", "up", "-d", "app"))

$dbPath = Join-Path $TARGET "data\app.db"
if (Test-Path $dbPath) {
    Write-Warn "Existing data\app.db found - first-run admin create only appears when there are zero users."
    Write-Warn "To reset first-run: stop the stack, delete data\app.db (+ -wal/-shm), then docker compose up -d."
}

Write-Ok ""
Write-Ok "Install complete."
Write-Host ""
Write-Host "Next steps:"
Write-Host ("  1. Open " + $APP_URL.TrimEnd('/') + "/login  or  http://127.0.0.1:8085/login")
Write-Host "  2. Create the admin account (shown automatically when the DB has zero users)"
Write-Host "  3. Create library + offline PIN, then /admin/setup"
if ($useBundled) {
    Write-Host "     Stack step should show Using bundled stack (keys already synced) - Continue"
}
else {
    Write-Host "     Stack step: ABS / Kavita / LibraForge presets + soft health probes"
}
Write-Host "  4. Optional Open Library catalog from that wizard (skip freely - seed cache is enough)"
Write-Host "  5. Optional Mullvad later: WireGuard keys + add vpn to COMPOSE_PROFILES"
Write-Host ""
Write-Host ("Stack dir: " + $TARGET)
Write-Host ("Logs:      Set-Location '" + $TARGET + "'; docker compose logs -f app")
if ($useBundled) {
    Write-Host "Ports:     app 8085 | ABS 13378 | Kavita 5000 | LibraForge 5056 | prowlarr 9696 | flare 8191 | jackett 9117"
}
else {
    Write-Host "Ports:     app 8085 | prowlarr 9696 | flaresolverr 8191 | jackett 9117"
}
Write-Host ""
Write-Host "Note: Linux host cron helpers are skipped on Windows."
Write-Host "      Use Task Scheduler or Admin -> Catalog schedule instead."
