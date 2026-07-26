# Bootstrap Kavita (bundled-media) and copy API key / library id into .env.
# Usage: .\scripts\sync_kavita_env.ps1 [-RepoRoot <path>] [-BaseUrl http://127.0.0.1:5000]
param(
    [string]$RepoRoot = "",
    [string]$BaseUrl = "http://127.0.0.1:5000",
    [string]$InternalUrl = "http://kavita:5000",
    [int]$WaitSeconds = 240
)

$ErrorActionPreference = "Continue"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "skip kavita env (no .env)"
    exit 0
}

function Get-EnvValue([string]$Key) {
    foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
        if ($line -match ("^" + [regex]::Escape($Key) + "=(.*)$")) {
            return $Matches[1].Trim()
        }
    }
    return ""
}

function Set-EnvKey([string]$Key, [string]$Value) {
    if ((Get-Item -LiteralPath $envFile).Length -gt 2MB) {
        throw ".env unexpectedly large"
    }
    $lines = [System.IO.File]::ReadAllLines($envFile)
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
    if (-not $found) { [void]$out.Add("$Key=$Value") }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($envFile, $out.ToArray(), $utf8NoBom)
}

function New-LocalSecret {
    $bytes = New-Object byte[] 18
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([System.BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
}

Write-Host "Waiting for Kavita at $BaseUrl ..."
$ready = $false
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "$BaseUrl/api/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    }
    catch {
        try {
            $r2 = Invoke-WebRequest -Uri "$BaseUrl/" -UseBasicParsing -TimeoutSec 3
            if ($r2.StatusCode -lt 500) { $ready = $true; break }
        }
        catch { }
    }
    Start-Sleep -Seconds 3
}
if (-not $ready) {
    Write-Host "skip kavita env (health timeout)"
    exit 0
}

$user = Get-EnvValue "BUNDLED_KAVITA_USERNAME"
if (-not $user) { $user = "admin" }
$pass = Get-EnvValue "BUNDLED_KAVITA_PASSWORD"
$apiKey = ""

# First-run register (no-op if admin already exists).
if (-not $pass) { $pass = New-LocalSecret }
try {
    $regBody = @{
        username = $user
        password = $pass
        email    = ""
    } | ConvertTo-Json
    Invoke-RestMethod -Uri "$BaseUrl/api/Account/register" -Method Post `
        -ContentType "application/json" -Body $regBody -TimeoutSec 30 | Out-Null
    Set-EnvKey "BUNDLED_KAVITA_USERNAME" $user
    Set-EnvKey "BUNDLED_KAVITA_PASSWORD" $pass
    Write-Host "Kavita admin registered ($user)"
}
catch {
    # Already initialized — fall back to stored password.
    $stored = Get-EnvValue "BUNDLED_KAVITA_PASSWORD"
    if ($stored) { $pass = $stored }
    else {
        Write-Host "skip kavita env (register skipped and no BUNDLED_KAVITA_PASSWORD)"
        exit 0
    }
}

$script:kavitaJwt = ""
try {
    $loginBody = @{ username = $user; password = $pass } | ConvertTo-Json
    $login = Invoke-RestMethod -Uri "$BaseUrl/api/Account/login" -Method Post `
        -ContentType "application/json" -Body $loginBody -TimeoutSec 30
    $apiKey = [string]$login.apiKey
    if (-not $apiKey) { $apiKey = [string]$login.ApiKey }
    $script:kavitaJwt = [string]$login.token
    if (-not $script:kavitaJwt) { $script:kavitaJwt = [string]$login.Token }
}
catch {
    Write-Host "skip kavita env (login failed)"
    exit 0
}
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    Write-Host "skip kavita env (empty api key)"
    exit 0
}

$headers = @{ "x-api-key" = $apiKey }
$libraryId = ""
try {
    $libs = Invoke-RestMethod -Uri "$BaseUrl/api/Library/libraries" -Headers $headers -TimeoutSec 20
    $existing = @($libs) | Where-Object {
        ($_.folders -contains "/ebooks") -or ($_.name -eq "Ebooks")
    } | Select-Object -First 1
    if ($existing) {
        $libraryId = [string]$existing.id
    }
    else {
        # LibraryType.Book = 2; FileTypeGroup Archive/Epub/Pdf = 1/2/3
        $createBody = @{
            name                 = "Ebooks"
            type                 = 2
            folders              = @("/ebooks")
            folderWatching       = $true
            includeInDashboard   = $true
            includeInRecommended = $true
            includeInSearch      = $true
            manageCollections    = $true
            manageReadingLists   = $true
            allowScrobbling      = $false
            excludePatterns      = @("**/unorganized/**", "unorganized/**", "**/unorganized", "unorganized")
            fileGroupTypes       = @(1, 2, 3)
        } | ConvertTo-Json -Depth 5
        # Prefer JWT if login returned a token (create validates FileGroupTypes).
        $createHeaders = $headers
        if ($script:kavitaJwt) {
            $createHeaders = @{ Authorization = "Bearer $($script:kavitaJwt)" }
        }
        $created = Invoke-RestMethod -Uri "$BaseUrl/api/Library/create" -Method Post -Headers $createHeaders `
            -ContentType "application/json" -Body $createBody -TimeoutSec 30
        $libraryId = [string]$created.id
        Write-Host "Kavita library created (/ebooks, excludes unorganized)"
    }
}
catch {
    Write-Host "warn kavita library ensure: $($_.Exception.Message)"
}

Set-EnvKey "KAVITA_URL" $InternalUrl
Set-EnvKey "KAVITA_API_KEY" $apiKey
if ($libraryId) { Set-EnvKey "KAVITA_LIBRARY_ID" $libraryId }
Write-Host "KAVITA_URL / KAVITA_API_KEY configured (internal $InternalUrl)"
