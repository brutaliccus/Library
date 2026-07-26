# Copy Jackett API key from ServerConfig.json into .env (repo-relative).
# Usage: .\scripts\sync_jackett_env.ps1 [-RepoRoot <path>]
param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Continue"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

$cfg = Join-Path $RepoRoot "jackett-config\Jackett\ServerConfig.json"
$envFile = Join-Path $RepoRoot ".env"

if (-not (Test-Path $cfg)) {
    Write-Host "skip jackett env (no config yet)"
    exit 0
}
if (-not (Test-Path $envFile)) {
    Write-Host "skip jackett env (no .env)"
    exit 0
}

try {
    $json = Get-Content -Raw -Path $cfg | ConvertFrom-Json
    $key = [string]$json.APIKey
}
catch {
    Write-Host "skip jackett env (unreadable config)"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($key)) {
    Write-Host "skip jackett env (empty API key)"
    exit 0
}

if ((Get-Item -LiteralPath $envFile).Length -gt 2MB) {
    Write-Host "skip jackett env (.env unexpectedly large)"
    exit 0
}
$lines = [System.IO.File]::ReadAllLines($envFile)
$found = $false
$out = New-Object System.Collections.Generic.List[string]
foreach ($line in $lines) {
    if ($line -match '^JACKETT_API_KEY=') {
        $found = $true
        [void]$out.Add("JACKETT_API_KEY=$key")
    }
    else {
        [void]$out.Add($line)
    }
}
if (-not $found) {
    [void]$out.Add("JACKETT_API_KEY=$key")
}
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($envFile, $out.ToArray(), $utf8NoBom)
Write-Host "JACKETT_API_KEY configured"

docker inspect audiobook-jackett 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    docker restart audiobook-jackett 2>$null | Out-Null
}
