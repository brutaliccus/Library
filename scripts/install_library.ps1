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
    [switch]$SkipNpm,
    [switch]$SkipJackett,
    [switch]$SkipProwlarr,
    [string]$JackettUrl = "",
    [string]$JackettApiKey = "",
    [string]$ProwlarrUrl = "",
    [string]$ProwlarrApiKey = "",
    [ValidateSet("", "download", "build", "skip")]
    [string]$OlMode = "",
    [string]$NpmDomain = "",
    [string]$NpmAbsDomain = "",
    [string]$NpmKavitaDomain = "",
    [string]$NpmLetsEncryptEmail = "",
    [string]$NpmAdminEmail = "admin@example.com",
    [string]$NpmAdminPassword = "",
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

Write-Step "==> Core settings [REQUIRED]"
Write-Host "    APP_URL = public URL friends open (invite links / CORS / push)." -ForegroundColor DarkGray
Write-Host "    SECRET_KEY = JWT signing secret. DATABASE_URL defaults to SQLite under ./data." -ForegroundColor DarkGray
$APP_URL = Read-Default "Public site URL [REQUIRED]" $AppUrl
if (-not $SecretKey) { $SecretKey = New-SecretKey }
$SECRET_KEY = if ($NonInteractive) { $SecretKey } else { Read-Default "Secret key [REQUIRED]" $SecretKey }
Set-EnvKey $envPath "APP_URL" $APP_URL
Set-EnvKey $envPath "SECRET_KEY" $SECRET_KEY
Set-EnvKey $envPath "DATABASE_URL" "sqlite+aiosqlite:///data/app.db"
Set-EnvKey $envPath "TZ" "UTC"
Set-EnvKey $envPath "PUID" "1000"
Set-EnvKey $envPath "PGID" "1000"
Set-EnvKey $envPath "AUDIOBOOK_DIR" "/audiobooks"
Set-EnvKey $envPath "EBOOK_DIR" "/ebooks"
Set-EnvKey $envPath "AUDIOBOOK_STAGING_DIRNAME" ".unorganized"
Set-EnvKey $envPath "AUDIOBOOK_STAGING_LEGACY_DIRNAME" "_unorganized"
Set-EnvKey $envPath "EBOOK_STAGING_DIRNAME" "unorganized"
# Admin Health Start/Stop/Restart — host docker group GID (WSL/Linux often 998/999).
$dockerGid = "998"
try {
  $gidLine = & getent group docker 2>$null
  if ($gidLine -match ":(\d+):") { $dockerGid = $Matches[1] }
} catch { }
Set-EnvKey $envPath "DOCKER_GID" $dockerGid
Set-EnvKey $envPath "FLARESOLVERR_URL" "http://flaresolverr:8191"

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
    $lfIp = ""
    try {
        $lfIp = (Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
            Select-Object -First 1 -ExpandProperty IPAddress)
    } catch {}
    if (-not $lfIp) { $lfIp = "127.0.0.1" }
    Set-EnvKey $envPath "LIBRAFORGE_URL" ("http://{0}:5056" -f $lfIp)
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
        $lfIp = ""
    try {
        $lfIp = (Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
            Select-Object -First 1 -ExpandProperty IPAddress)
    } catch {}
    if (-not $lfIp) { $lfIp = "127.0.0.1" }
    Set-EnvKey $envPath "LIBRAFORGE_URL" ("http://{0}:5056" -f $lfIp)
    }
    if (-not (Get-EnvKeyValue $envPath "LIBRAFORGE_INTERNAL_URL")) {
        Set-EnvKey $envPath "LIBRAFORGE_INTERNAL_URL" "http://host.docker.internal:5056"
    }
}
Set-EnvKey $envPath "LIBRAFORGE_M4B_JOBS" "1"
Set-EnvKey $envPath "LIBRAFORGE_PIPELINE_ENABLED" ($(if ($lfOn) { "true" } else { "false" }))
Set-EnvKey $envPath "EBOOK_PIPELINE_ENABLED" ($(if ($ebOn) { "true" } else { "false" }))
if (-not (Get-EnvKeyValue $envPath "LIBRAFORGE_MIN_SCORE")) { Set-EnvKey $envPath "LIBRAFORGE_MIN_SCORE" "0.70" }
if (-not (Get-EnvKeyValue $envPath "EBOOK_MIN_SCORE")) { Set-EnvKey $envPath "EBOOK_MIN_SCORE" "0.70" }
if (-not (Get-EnvKeyValue $envPath "LIBRARY_SWEEP_ABS_SCAN_EVERY")) { Set-EnvKey $envPath "LIBRARY_SWEEP_ABS_SCAN_EVERY" "25" }
if (-not (Get-EnvKeyValue $envPath "EBOOK_SWEEP_KAVITA_SCAN_EVERY")) { Set-EnvKey $envPath "EBOOK_SWEEP_KAVITA_SCAN_EVERY" "25" }
if (-not (Get-EnvKeyValue $envPath "EBOOK_SWEEP_CONVERT_ALL_TO_EPUB")) { Set-EnvKey $envPath "EBOOK_SWEEP_CONVERT_ALL_TO_EPUB" "true" }
if (-not (Get-EnvKeyValue $envPath "EBOOK_SWEEP_FORCE_METADATA")) { Set-EnvKey $envPath "EBOOK_SWEEP_FORCE_METADATA" "true" }
if (-not (Get-EnvKeyValue $envPath "OPENROUTER_ENABLED")) { Set-EnvKey $envPath "OPENROUTER_ENABLED" "false" }

if ($useBundled -and -not $NonInteractive) {
    Write-Step "==> Debrid providers [OPTIONAL]"
    Write-Host "    Server defaults; users can also set keys per library. TorBox needs no qBittorrent container." -ForegroundColor DarkGray
    $rd = Read-Default "Real-Debrid API token (Enter to skip)" (Get-EnvKeyValue $envPath "REAL_DEBRID_API_TOKEN")
    $tor = Read-Default "TorBox API token (Enter to skip)" (Get-EnvKeyValue $envPath "TORBOX_API_TOKEN")
    if ($rd) { Set-EnvKey $envPath "REAL_DEBRID_API_TOKEN" $rd }
    if ($tor) { Set-EnvKey $envPath "TORBOX_API_TOKEN" $tor }
}

$apk = Read-Default "GitHub owner/repo for Library APK releases" $ApkRepo
Set-EnvKey $envPath "ANDROID_APK_GITHUB_REPO" $apk
if (-not (Get-EnvKeyValue $envPath "ANDROID_MIN_VERSION_CODE")) { Set-EnvKey $envPath "ANDROID_MIN_VERSION_CODE" "59" }
if (-not (Get-EnvKeyValue $envPath "ANDROID_FORCE_UPDATES")) { Set-EnvKey $envPath "ANDROID_FORCE_UPDATES" "true" }

Write-Step "==> Scraper mode [RECOMMENDED: RSS-only]"
$deep = if ($NonInteractive) { [bool]$EnableDeepScrapers } else {
    Write-Warn "Deep FlareSolverr crawls are HIGH USAGE."
    Write-Host "Recommended: RSS-only (ABB + Knaben) - live Jackett search still works."
    Write-Host "Compose caps FlareSolverr (768m RAM / 1.5 CPU / 200 pids)." -ForegroundColor DarkGray
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

# Jackett — bundled + ABB preconfigure by default; connect existing if preferred.
Write-Step "==> Jackett (AudioBook Bay Torznab) [RECOMMENDED]"
Write-Host "Bundled Jackett is preconfigured for AudioBookBay + FlareSolverr." -ForegroundColor DarkGray
Write-Host "Already run Jackett elsewhere? Connect URL + API key instead." -ForegroundColor DarkGray
$useBundledJackett = -not [bool]$SkipJackett
if ($JackettUrl -and $JackettApiKey) {
    $useBundledJackett = $false
}
elseif (-not $NonInteractive) {
    $useBundledJackett = Read-YesNo "Deploy + preconfigure bundled Jackett? (Already have Jackett? answer n)" $true
}
if ($useBundledJackett) {
    Set-EnvKey $envPath "JACKETT_URL" "http://audiobook-jackett:9117"
    Write-Ok "Bundled Jackett — ABB indexer + FlareSolverr wired after first start"
}
else {
    $JackettUrl = Read-Default "Existing Jackett URL" $(if ($JackettUrl) { $JackettUrl } else { Get-EnvKeyValue $envPath "JACKETT_URL" })
    $JackettApiKey = Read-Default "Existing Jackett API key" $(if ($JackettApiKey) { $JackettApiKey } else { Get-EnvKeyValue $envPath "JACKETT_API_KEY" })
    if ($JackettUrl -and $JackettApiKey) {
        Set-EnvKey $envPath "JACKETT_URL" $JackettUrl
        Set-EnvKey $envPath "JACKETT_API_KEY" $JackettApiKey
        Write-Ok "External Jackett credentials saved"
    }
    else {
        Write-Warn "No Jackett URL/key — falling back to bundled Jackett"
        $useBundledJackett = $true
        Set-EnvKey $envPath "JACKETT_URL" "http://audiobook-jackett:9117"
    }
}

# Prowlarr — Knaben + ABB Torznab (matches production Pi).
Write-Step "==> Prowlarr (ABB + Knaben indexers) [RECOMMENDED]"
Write-Host "Bundled Prowlarr gets native Knaben + AudioBookBay Torznab -> Jackett." -ForegroundColor DarkGray
$useBundledProwlarr = -not [bool]$SkipProwlarr
if ($ProwlarrUrl -and $ProwlarrApiKey) {
    $useBundledProwlarr = $false
}
elseif (-not $NonInteractive) {
    $useBundledProwlarr = Read-YesNo "Deploy + preconfigure bundled Prowlarr? (Already have Prowlarr? answer n)" $true
}
if ($useBundledProwlarr) {
    Set-EnvKey $envPath "PROWLARR_URL" "http://prowlarr:9696"
    Write-Ok "Bundled Prowlarr — Knaben + ABB wired after first start"
}
else {
    $ProwlarrUrl = Read-Default "Existing Prowlarr URL" $(if ($ProwlarrUrl) { $ProwlarrUrl } else { Get-EnvKeyValue $envPath "PROWLARR_URL" })
    $ProwlarrApiKey = Read-Default "Existing Prowlarr API key" $(if ($ProwlarrApiKey) { $ProwlarrApiKey } else { Get-EnvKeyValue $envPath "PROWLARR_API_KEY" })
    if ($ProwlarrUrl -and $ProwlarrApiKey) {
        Set-EnvKey $envPath "PROWLARR_URL" $ProwlarrUrl
        Set-EnvKey $envPath "PROWLARR_API_KEY" $ProwlarrApiKey
        Write-Ok "External Prowlarr credentials saved"
    }
    else {
        Write-Warn "No Prowlarr URL/key — falling back to bundled Prowlarr"
        $useBundledProwlarr = $true
        Set-EnvKey $envPath "PROWLARR_URL" "http://prowlarr:9696"
    }
}

# Open Library catalog — skip by default; indexers + indexer_cache seed are day-one search.
Write-Step "==> Open Library catalog [ADVANCED / OPTIONAL]"
Write-Host "Day-one search uses Jackett/Prowlarr + indexer cache seed (~36 MB) — not Open Library." -ForegroundColor DarkGray
Write-Host "A local OL SQLite DB is multi-GB and optional (Admin -> Catalog later). Skip is recommended." -ForegroundColor DarkGray
if (-not $OlMode) {
    if ($NonInteractive) {
        $OlMode = "skip"
    }
    else {
        Write-Host "  [1] Skip for now (recommended — indexers cover search)"
        Write-Host "  [2] Build locally from Open Library dumps (hours + multi-GB disk)"
        Write-Host "  [3] Download a prebuilt OL DB if published (very large; usually unavailable)"
        $choice = Read-Default "Open Library catalog setup" "1"
        switch -Regex ($choice) {
            '^(2|b|build)$' { $OlMode = "build" }
            '^(3|d|download|prebuilt)$' { $OlMode = "download" }
            Default { $OlMode = "skip" }
        }
    }
}
if (-not (Get-EnvKeyValue $envPath "OL_CATALOG_DB_PATH")) {
    Set-EnvKey $envPath "OL_CATALOG_DB_PATH" "/app/data/ol_catalog.db"
}
if (-not (Get-EnvKeyValue $envPath "OL_DUMPS_DIR")) {
    Set-EnvKey $envPath "OL_DUMPS_DIR" "/openlibrary/dumps"
}
Write-Ok "Open Library mode: $OlMode"

# Nginx Proxy Manager — default on; skip only if you already reverse-proxy on 80/443.
Write-Step "==> Nginx Proxy Manager (reverse proxy) [RECOMMENDED]"
Write-Host "Remote HTTPS needs a reverse proxy. Fresh installs start NPM (compose profile npm)." -ForegroundColor DarkGray
Write-Host "Answer No only if you already run NPM / Caddy / Traefik / nginx on ports 80/443." -ForegroundColor DarkGray
$existingProfiles = Get-EnvKeyValue $envPath "COMPOSE_PROFILES"
$npmDefault = $true
if ($existingProfiles -match '(^|,)\s*npm\s*(,|$)') { $npmDefault = $true }
$useNpm = -not [bool]$SkipNpm
if ($SkipNpm) {
    $useNpm = $false
    Write-Warn "SkipNpm - Nginx Proxy Manager off"
}
elseif (-not $NonInteractive) {
    $useNpm = Read-YesNo "Enable Nginx Proxy Manager (publishes 80/443 + admin :81)?" $npmDefault
}
if ($useNpm) {
    Write-Host "Ports 80 + 443 (public) and 81 (NPM admin). Container: library-npm." -ForegroundColor DarkGray
    if (-not $NpmDomain) { $NpmDomain = Get-EnvKeyValue $envPath "NPM_DOMAIN" }
    if (-not $NpmAbsDomain) { $NpmAbsDomain = Get-EnvKeyValue $envPath "NPM_ABS_DOMAIN" }
    if (-not $NpmKavitaDomain) { $NpmKavitaDomain = Get-EnvKeyValue $envPath "NPM_KAVITA_DOMAIN" }
    if (-not $NpmLetsEncryptEmail) { $NpmLetsEncryptEmail = Get-EnvKeyValue $envPath "NPM_LETSENCRYPT_EMAIL" }
    $existingAdminEmail = Get-EnvKeyValue $envPath "NPM_ADMIN_EMAIL"
    if ($NpmAdminEmail -eq "admin@example.com" -and $existingAdminEmail) { $NpmAdminEmail = $existingAdminEmail }
    $NpmDomain = Read-Default "Library public domain (blank = LAN / configure hosts later)" $NpmDomain
    if ($useBundled) {
        $NpmAbsDomain = Read-Default "Audiobookshelf domain (optional)" $NpmAbsDomain
        $NpmKavitaDomain = Read-Default "Kavita domain (optional)" $NpmKavitaDomain
    }
    $NpmLetsEncryptEmail = Read-Default "Let's Encrypt email (blank = HTTP only)" $NpmLetsEncryptEmail
    $NpmAdminEmail = Read-Default "NPM admin email" $NpmAdminEmail
    if (-not $NpmAdminPassword) {
        $existingNpmPass = Get-EnvKeyValue $envPath "NPM_ADMIN_PASSWORD"
        if ($existingNpmPass -and $existingNpmPass -ne "changeme") {
            $NpmAdminPassword = $existingNpmPass
        }
        else {
            $NpmAdminPassword = New-SecretKey
        }
    }
    $NpmAdminPassword = Read-Default "NPM admin password" $NpmAdminPassword
    Set-EnvKey $envPath "NPM_ADMIN_EMAIL" $NpmAdminEmail
    Set-EnvKey $envPath "NPM_ADMIN_PASSWORD" $NpmAdminPassword
    Set-EnvKey $envPath "NPM_DOMAIN" $NpmDomain
    Set-EnvKey $envPath "NPM_ABS_DOMAIN" $NpmAbsDomain
    Set-EnvKey $envPath "NPM_KAVITA_DOMAIN" $NpmKavitaDomain
    Set-EnvKey $envPath "NPM_LETSENCRYPT_EMAIL" $NpmLetsEncryptEmail
    Set-EnvKey $envPath "NPM_DISABLE_IPV6" "true"
    if ($NpmDomain) {
        if ($NpmLetsEncryptEmail) {
            Set-EnvKey $envPath "APP_URL" "https://$NpmDomain"
            $APP_URL = "https://$NpmDomain"
            Write-Ok "APP_URL -> https://$NpmDomain (Let's Encrypt after DNS points here)"
        }
        else {
            Set-EnvKey $envPath "APP_URL" "http://$NpmDomain"
            $APP_URL = "http://$NpmDomain"
            Write-Ok "APP_URL -> http://$NpmDomain (HTTP; add LE email later for HTTPS)"
        }
    }
    else {
        Write-Warn "No domain - NPM still starts; admin on :81 + LAN HTTP proxy on :80."
        Write-Warn "Later: set NPM_DOMAIN in .env, then .\scripts\configure_npm.ps1"
    }
}
else {
    Write-Warn "Skipped NPM. For remote HTTPS later, point your reverse proxy at http://127.0.0.1:8085"
    Write-Warn "APP_URL should be the public https:// URL friends open."
}

# VPN / gluetun is optional and OFF by default on Windows (Mullvad not required).
$vpn = [bool]$EnableVpn
if (-not $NonInteractive) {
    $vpn = Read-YesNo "Enable Mullvad VPN sidecar (gluetun) now? Optional - not required. Needs WireGuard keys." $false
}
# Rebuild known profiles; preserve any other COMPOSE_PROFILES entries.
$known = @("bundled-media", "npm", "vpn")
$otherProfiles = @()
if ($existingProfiles) {
    foreach ($piece in ($existingProfiles -split ',')) {
        $t = $piece.Trim()
        if ($t -and ($known -notcontains $t)) { $otherProfiles += $t }
    }
}
$profileParts = @()
if ($useBundled) { $profileParts += "bundled-media" }
if ($useNpm) { $profileParts += "npm" }
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
$profileParts += $otherProfiles
$mergedProfiles = Merge-ComposeProfiles $profileParts
Set-EnvKey $envPath "COMPOSE_PROFILES" $mergedProfiles
$env:COMPOSE_PROFILES = $mergedProfiles
$composeProfileArgs = @()
foreach ($p in ($mergedProfiles -split ',')) {
    $t = $p.Trim()
    if ($t) { $composeProfileArgs += @("--profile", $t) }
}
$profileNote = @()
if ($useBundled) { $profileNote += "bundled-media" }
if ($useNpm) { $profileNote += "npm" }
if ($vpn) { $profileNote += "vpn" }
if ($mergedProfiles) {
    Write-Ok "COMPOSE_PROFILES=$mergedProfiles ($($profileNote -join ', '))"
}
else {
    Write-Warn "COMPOSE_PROFILES empty - core stack only (no bundled-media / npm / vpn)"
}

foreach ($d in @(
        "data", "prowlarr-config", "jackett-config",
        "audiobookshelf-config", "audiobookshelf-metadata", "kavita-config",
        "libraforge-auth", "libraforge-config", "libraforge-reports",
        "npm-data", "npm-letsencrypt",
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

Write-Step "==> Open Library catalog action"
switch ($OlMode.ToLowerInvariant()) {
    { $_ -in @("download", "prebuilt", "d") } {
        $olDb = Join-Path $TARGET "data\ol_catalog.db"
        if ((Test-Path $olDb) -and ((Get-Item $olDb).Length -gt 1MB)) {
            Write-Ok "OL catalog already present at data\ol_catalog.db"
        }
        else {
            $fetch = Join-Path $TARGET "scripts\fetch_ol_catalog.ps1"
            if (Test-Path $fetch) {
                Write-Host "Attempting optional prebuilt Open Library catalog download (large; soft-fail if missing) ..."
                & powershell -ExecutionPolicy Bypass -File $fetch -RepoRoot $TARGET
                if ($LASTEXITCODE -ne 0) {
                    Write-Warn "No prebuilt OL DB on the release — continuing. Indexer search still works."
                    Write-Warn "Advanced: Admin -> Catalog, or scripts/ol_import_dumps.py / scripts/fetch_ol_catalog.ps1 later."
                }
            }
        }
    }
    { $_ -in @("build", "b") } {
        Write-Warn "Local OL build starts after the app container is up (can take many hours)."
    }
    Default {
        Write-Host "Skipped Open Library catalog — configure later in Admin -> Catalog." -ForegroundColor DarkGray
    }
}

Write-Step "==> Starting Docker stack"
Write-Warn "First boot imports seed/indexer_cache.db.gz into an empty DB (~150 MB). This may take a few minutes."
if ($useBundled) {
    Write-Warn "First LibraForge image build can take several minutes."
    Write-Host "Bundled media keys sync automatically after services are healthy."
}
else {
    Write-Host "After create-admin / create-library / offline PIN, /admin/setup configures ABS, Kavita, and LibraForge."
}
if ($useNpm) {
    Write-Warn "Starting Nginx Proxy Manager (library-npm) on 80/443/81 - required when Enable NPM = Yes."
}
$upArgs = @("compose") + $composeProfileArgs + @("up", "-d")
if (-not $SkipBuild) { $upArgs += "--build" }
$upCode = Invoke-Compose $upArgs
if ($upCode -ne 0) {
    Write-Err "docker compose up failed - check: docker compose logs"
    exit 1
}
if ($useNpm) {
    Write-Host "Ensuring library-npm is up (compose profile npm) ..."
    [void](Invoke-Compose @("compose", "--profile", "npm", "up", "-d", "nginx-proxy-manager"))
}
if (-not $useBundledJackett) {
    Write-Warn "Stopping bundled Jackett (using external JACKETT_URL)"
    [void](Invoke-Compose @("compose", "stop", "jackett"))
}
if (-not $useBundledProwlarr) {
    Write-Warn "Stopping bundled Prowlarr (using external PROWLARR_URL)"
    [void](Invoke-Compose @("compose", "stop", "prowlarr"))
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

Write-Step "==> Configure Jackett / Prowlarr / sync keys"
$jackCfg = Join-Path $TARGET "scripts\configure_jackett.ps1"
$prowlCfg = Join-Path $TARGET "scripts\configure_prowlarr.ps1"
if ($useBundledJackett) {
    if (Test-Path $jackCfg) {
        Write-Host "Preconfiguring Jackett (FlareSolverr + AudioBookBay)"
        & powershell -ExecutionPolicy Bypass -File $jackCfg -RepoRoot $TARGET -ForceBundled
    }
    else {
        $syncPs1 = Join-Path $TARGET "scripts\sync_jackett_env.ps1"
        if (Test-Path $syncPs1) { & powershell -ExecutionPolicy Bypass -File $syncPs1 -RepoRoot $TARGET }
    }
}
elseif (Test-Path $jackCfg) {
    & powershell -ExecutionPolicy Bypass -File $jackCfg -RepoRoot $TARGET `
        -ExternalUrl (Get-EnvKeyValue $envPath "JACKETT_URL") `
        -ExternalApiKey (Get-EnvKeyValue $envPath "JACKETT_API_KEY")
}
if ($useBundledProwlarr) {
    if (Test-Path $prowlCfg) {
        Write-Host "Preconfiguring Prowlarr (Knaben + AudioBookBay -> Jackett)"
        & powershell -ExecutionPolicy Bypass -File $prowlCfg -RepoRoot $TARGET -ForceBundled
    }
    else {
        $prowlSyncPs1 = Join-Path $TARGET "scripts\sync_prowlarr_env.ps1"
        if (Test-Path $prowlSyncPs1) { & powershell -ExecutionPolicy Bypass -File $prowlSyncPs1 -RepoRoot $TARGET }
    }
}
elseif (Test-Path $prowlCfg) {
    & powershell -ExecutionPolicy Bypass -File $prowlCfg -RepoRoot $TARGET `
        -ExternalUrl (Get-EnvKeyValue $envPath "PROWLARR_URL") `
        -ExternalApiKey (Get-EnvKeyValue $envPath "PROWLARR_API_KEY")
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

if ($useNpm) {
    Write-Step "==> Verify Nginx Proxy Manager is listening on :81"
    $npmOk = $false
    for ($i = 1; $i -le 60; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:81/" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 600) {
                $npmOk = $true
                Write-Ok "NPM admin port :81 is listening (HTTP $($r.StatusCode))"
                break
            }
        }
        catch {
            $resp = $_.Exception.Response
            if ($resp -and [int]$resp.StatusCode -ge 200) {
                $npmOk = $true
                Write-Ok "NPM admin port :81 is listening (HTTP $([int]$resp.StatusCode))"
                break
            }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $npmOk) {
        Write-Err "FATAL: Nginx Proxy Manager was enabled but http://127.0.0.1:81 is not listening."
        Write-Err "library-npm did not publish ports 80/443/81 - remote HTTPS cannot work."
        Write-Warn "Debug: docker compose --profile npm ps nginx-proxy-manager"
        Write-Warn "       docker compose --profile npm logs --tail=80 nginx-proxy-manager"
        Write-Warn "       grep COMPOSE_PROFILES= .env / Get-Content .env | Select-String COMPOSE_PROFILES"
        Write-Warn "Fix port conflicts, then: docker compose --profile npm up -d nginx-proxy-manager"
        Write-Warn "Or skip with -SkipNpm / LIBRARY_SKIP_NPM=1"
        exit 1
    }
    $npmCfg = Join-Path $TARGET "scripts\configure_npm.ps1"
    if (Test-Path $npmCfg) {
        Write-Step "==> Configuring Nginx Proxy Manager (admin + proxy hosts via API - no GUI required)"
        & powershell -ExecutionPolicy Bypass -File $npmCfg -RepoRoot $TARGET
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "configure_npm.ps1 reported errors - NPM is up; re-run .\scripts\configure_npm.ps1"
        }
        $updatedUrl = Get-EnvKeyValue $envPath "APP_URL"
        if ($updatedUrl) { $APP_URL = $updatedUrl }
    }
}

if ($OlMode -match '^(build|b)$') {
    Write-Host "Starting Open Library catalog build inside the app container (background) ..."
    docker compose exec -d -e PYTHONPATH=/app app python /app/scripts/ol_import_dumps.py 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Could not start OL build — run later: docker compose exec app python /app/scripts/ol_import_dumps.py"
    }
}

# Force-recreate app + seed app_settings so Admin Overview sees Jackett/Prowlarr keys.
$applyKeys = Join-Path $TARGET "scripts\apply_indexer_keys.ps1"
if (Test-Path $applyKeys) {
    Write-Step "==> Applying Jackett/Prowlarr keys into running app"
    & powershell -ExecutionPolicy Bypass -File $applyKeys -RepoRoot $TARGET
    if ($LASTEXITCODE -ne 0) {
        Write-Err "FATAL: Jackett/Prowlarr API keys missing from .env / app Settings."
        Write-Warn "Repair: .\scripts\configure_jackett.ps1 -ForceBundled; .\scripts\configure_prowlarr.ps1 -ForceBundled; .\scripts\apply_indexer_keys.ps1"
        $script:IndexerCfgFail = $true
    }
}
else {
    [void](Invoke-Compose @("compose", "up", "-d", "--force-recreate", "--no-deps", "app"))
}

Write-Step "==> Post-install health report"
function Probe-Http([string]$Name, [string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) {
            Write-Host ("  {0,-16} OK" -f $Name) -ForegroundColor Green
            return
        }
    } catch {}
    Write-Host ("  {0,-16} warming / unreachable" -f $Name) -ForegroundColor Yellow
}
if ($healthy) { Write-Host ("  {0,-16} OK" -f "app") -ForegroundColor Green }
else { Probe-Http "app" "http://127.0.0.1:8085/api/health" }
if ($useBundledProwlarr) { Probe-Http "prowlarr" "http://127.0.0.1:9696/ping" }
else { Write-Host ("  {0,-16} external" -f "prowlarr") -ForegroundColor Yellow }
if ($useBundledJackett) { Probe-Http "jackett" "http://127.0.0.1:9117/" }
else { Write-Host ("  {0,-16} external" -f "jackett") -ForegroundColor Yellow }
Probe-Http "flaresolverr" "http://127.0.0.1:8191/"
if ($useBundled) {
    Probe-Http "audiobookshelf" "http://127.0.0.1:13378/"
    Probe-Http "kavita" "http://127.0.0.1:5000/"
    Probe-Http "libraforge" "http://127.0.0.1:5056/health"
}
if ($useNpm) {
    Probe-Http "npm-admin" "http://127.0.0.1:81/"
    Probe-Http "npm-proxy:80" "http://127.0.0.1/"
}
$olDbPath = Join-Path $TARGET "data\ol_catalog.db"
if ((Test-Path $olDbPath) -and ((Get-Item $olDbPath).Length -gt 1MB)) {
    $mb = [math]::Round((Get-Item $olDbPath).Length / 1MB)
    Write-Host ("  {0,-16} OK ({1} MB)" -f "ol-catalog", $mb) -ForegroundColor Green
}
else {
    Write-Host ("  {0,-16} absent (optional)" -f "ol-catalog") -ForegroundColor Yellow
}
$jk = Get-EnvKeyValue $envPath "JACKETT_API_KEY"
$pk = Get-EnvKeyValue $envPath "PROWLARR_API_KEY"
$keysOk = $true
if ($jk -and $jk -notmatch 'your-') { Write-Host ("  {0,-16} OK" -f "jackett-key") -ForegroundColor Green }
else { Write-Host ("  {0,-16} missing" -f "jackett-key") -ForegroundColor Red; $keysOk = $false }
if ($pk -and $pk -notmatch 'your-') { Write-Host ("  {0,-16} OK" -f "prowlarr-key") -ForegroundColor Green }
else { Write-Host ("  {0,-16} missing" -f "prowlarr-key") -ForegroundColor Red; $keysOk = $false }
if (-not $keysOk -or $script:IndexerCfgFail) {
    Write-Host ""
    Write-Host "**********************************************************************" -ForegroundColor Red
    Write-Host "* Jackett/Prowlarr keys missing. Admin Overview = Not configured.   *" -ForegroundColor Red
    Write-Host "*   .\scripts\configure_jackett.ps1 -ForceBundled                    *" -ForegroundColor Red
    Write-Host "*   .\scripts\configure_prowlarr.ps1 -ForceBundled                   *" -ForegroundColor Red
    Write-Host "*   .\scripts\apply_indexer_keys.ps1                                 *" -ForegroundColor Red
    Write-Host "**********************************************************************" -ForegroundColor Red
}

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
Write-Host "  4. Optional Mullvad later: WireGuard keys + add vpn to COMPOSE_PROFILES"
Write-Host ""
Write-Host "Indexers (auto-configured when bundled):"
Write-Host ("  - Jackett: " + (Get-EnvKeyValue $envPath "JACKETT_URL"))
Write-Host ("  - Prowlarr: " + (Get-EnvKeyValue $envPath "PROWLARR_URL"))
Write-Host "  - Re-run: .\scripts\configure_jackett.ps1 -ForceBundled; .\scripts\configure_prowlarr.ps1 -ForceBundled; .\scripts\apply_indexer_keys.ps1"
$olDb = Join-Path $TARGET "data\ol_catalog.db"
if (Test-Path $olDb) {
    Write-Host "  - Open Library: data\ol_catalog.db present"
}
elseif ($OlMode -match '^(build|b)$') {
    Write-Host "  - Open Library: local build running (docker compose logs -f app)"
}
else {
    Write-Host "  - Open Library: skipped (optional) — Admin -> Catalog later if you want a local OL DB"
}
if ($useNpm) {
    Write-Host ""
    Write-Host "Nginx Proxy Manager:"
    Write-Host "  - Admin UI: http://127.0.0.1:81  (NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD in .env)"
    $npmDom = Get-EnvKeyValue $envPath "NPM_DOMAIN"
    if ($npmDom) {
        Write-Host ("  - Library proxy: " + $APP_URL)
        Write-Host "  - DNS: point A/AAAA at this host for Let's Encrypt; re-run .\scripts\configure_npm.ps1"
    }
    else {
        Write-Host "  - LAN proxy hosts created for hostname/IP on :80 (also use :8085)"
        Write-Host "  - Set NPM_DOMAIN then .\scripts\configure_npm.ps1"
    }
}
else {
    Write-Host ""
    Write-Host "Reverse proxy: skipped. Remote HTTPS needs a proxy -> http://127.0.0.1:8085"
}
Write-Host ""
Write-Host ("Stack dir: " + $TARGET)
Write-Host ("Logs:      Set-Location '" + $TARGET + "'; docker compose logs -f app")
if ($useBundled) {
    $ports = "app 8085 | ABS 13378 | Kavita 5000 | LibraForge 5056 | prowlarr 9696 | flare 8191 | jackett 9117"
}
else {
    $ports = "app 8085 | prowlarr 9696 | flaresolverr 8191 | jackett 9117"
}
if ($useNpm) { $ports = "$ports | npm 80/443/81" }
Write-Host "Ports:     $ports"
Write-Host ""
Write-Host "Note: Linux host cron helpers are skipped on Windows."
Write-Host "      Use Task Scheduler or Admin -> Catalog schedule instead."
Write-Host "Updates: .\scripts\update_library.ps1  (fetch origin/main + rebuild app)"
Write-Host "Docs:    docs/ubuntu-server-install.md#updating"
