# Bootstrap Audiobookshelf (bundled-media) and copy API key / library id into .env.
# Usage: .\scripts\sync_abs_env.ps1 [-RepoRoot <path>] [-BaseUrl http://127.0.0.1:13378]
param(
    [string]$RepoRoot = "",
    [string]$BaseUrl = "http://127.0.0.1:13378",
    [string]$InternalUrl = "http://audiobookshelf:80",
    [int]$WaitSeconds = 180
)

$ErrorActionPreference = "Continue"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "skip abs env (no .env)"
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

Write-Host "Waiting for Audiobookshelf at $BaseUrl ..."
$ready = $false
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "$BaseUrl/healthcheck" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    }
    catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host "skip abs env (healthcheck timeout)"
    exit 0
}

try {
    $status = Invoke-RestMethod -Uri "$BaseUrl/status" -Method Get -TimeoutSec 10
}
catch {
    Write-Host "skip abs env (status unreachable)"
    exit 0
}

$user = Get-EnvValue "BUNDLED_ABS_USERNAME"
if (-not $user) { $user = "admin" }
$pass = Get-EnvValue "BUNDLED_ABS_PASSWORD"
$token = ""

if (-not $status.isInit) {
    if (-not $pass) { $pass = New-LocalSecret }
    try {
        $body = @{ newRoot = @{ username = $user; password = $pass } } | ConvertTo-Json -Depth 4
        Invoke-RestMethod -Uri "$BaseUrl/init" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 30 | Out-Null
        Set-EnvKey "BUNDLED_ABS_USERNAME" $user
        Set-EnvKey "BUNDLED_ABS_PASSWORD" $pass
        Write-Host "ABS root user initialized ($user)"
    }
    catch {
        Write-Host "skip abs env (init failed: $($_.Exception.Message))"
        exit 0
    }
}
elseif (-not $pass) {
    Write-Host "skip abs env (already initialized; set BUNDLED_ABS_PASSWORD or ABS_API_KEY manually)"
    exit 0
}

try {
    $loginBody = @{ username = $user; password = $pass } | ConvertTo-Json
    $login = Invoke-RestMethod -Uri "$BaseUrl/login" -Method Post -ContentType "application/json" -Body $loginBody -TimeoutSec 30
    $token = [string]$login.user.token
}
catch {
    Write-Host "skip abs env (login failed)"
    exit 0
}
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "skip abs env (empty token)"
    exit 0
}

$headers = @{ Authorization = "Bearer $token" }
$libraryId = ""
try {
    $libs = Invoke-RestMethod -Uri "$BaseUrl/api/libraries" -Headers $headers -TimeoutSec 20
    $existing = @($libs.libraries) | Where-Object {
        ($_.folders | ForEach-Object { $_.fullPath }) -contains "/audiobooks"
    } | Select-Object -First 1
    if (-not $existing) {
        $existing = @($libs.libraries) | Where-Object { $_.mediaType -eq "book" } | Select-Object -First 1
    }
    if ($existing) {
        $libraryId = [string]$existing.id
    }
    else {
        $createBody = @{
            name      = "Audiobooks"
            folders   = @(@{ fullPath = "/audiobooks" })
            mediaType = "book"
            provider  = "audible"
            icon      = "audiobookshelf"
        } | ConvertTo-Json -Depth 5
        $created = Invoke-RestMethod -Uri "$BaseUrl/api/libraries" -Method Post -Headers $headers `
            -ContentType "application/json" -Body $createBody -TimeoutSec 30
        $libraryId = [string]$created.id
        if (-not $libraryId -and $created.library) { $libraryId = [string]$created.library.id }
        Write-Host "ABS library created (/audiobooks)"
    }
}
catch {
    Write-Host "warn abs library ensure: $($_.Exception.Message)"
}

Set-EnvKey "ABS_URL" $InternalUrl
Set-EnvKey "ABS_API_KEY" $token
if ($libraryId) { Set-EnvKey "ABS_LIBRARY_ID" $libraryId }
Write-Host "ABS_URL / ABS_API_KEY configured (internal $InternalUrl)"
