# Copy Prowlarr API key from config.xml into .env (repo-relative).
# Usage: .\scripts\sync_prowlarr_env.ps1 [-RepoRoot <path>]
param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Continue"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

$cfg = Join-Path $RepoRoot "prowlarr-config\config.xml"
$envFile = Join-Path $RepoRoot ".env"

if (-not (Test-Path $cfg)) {
    Write-Host "skip prowlarr env (no config yet)"
    exit 0
}
if (-not (Test-Path $envFile)) {
    Write-Host "skip prowlarr env (no .env)"
    exit 0
}

try {
    [xml]$xml = Get-Content -Raw -Path $cfg
    $key = [string]$xml.Config.ApiKey
}
catch {
    Write-Host "skip prowlarr env (unreadable config)"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($key)) {
    Write-Host "skip prowlarr env (empty API key)"
    exit 0
}

if ((Get-Item -LiteralPath $envFile).Length -gt 2MB) {
    Write-Host "skip prowlarr env (.env unexpectedly large)"
    exit 0
}
$lines = [System.IO.File]::ReadAllLines($envFile)
$found = $false
$out = New-Object System.Collections.Generic.List[string]
foreach ($line in $lines) {
    if ($line -match '^PROWLARR_API_KEY=') {
        $found = $true
        [void]$out.Add("PROWLARR_API_KEY=$key")
    }
    else {
        [void]$out.Add($line)
    }
}
if (-not $found) {
    [void]$out.Add("PROWLARR_API_KEY=$key")
}
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($envFile, $out.ToArray(), $utf8NoBom)
Write-Host "PROWLARR_API_KEY configured"
