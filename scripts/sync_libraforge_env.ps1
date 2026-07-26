# Wire LibraForge URLs into .env for the bundled-media profile (no API key).
# Usage: .\scripts\sync_libraforge_env.ps1 [-RepoRoot <path>]
param(
    [string]$RepoRoot = "",
    [string]$PublicUrl = "http://127.0.0.1:5056",
    [string]$InternalUrl = "http://libraforge:5056",
    [int]$WaitSeconds = 180
)

$ErrorActionPreference = "Continue"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "skip libraforge env (no .env)"
    exit 0
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

Write-Host "Waiting for LibraForge at $PublicUrl ..."
$ready = $false
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "$PublicUrl/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    }
    catch {
        try {
            $r2 = Invoke-WebRequest -Uri "$PublicUrl/" -UseBasicParsing -TimeoutSec 3
            if ($r2.StatusCode -lt 500) { $ready = $true; break }
        }
        catch { }
    }
    Start-Sleep -Seconds 3
}
if (-not $ready) {
    Write-Host "skip libraforge env (health timeout)"
    exit 0
}

Set-EnvKey "LIBRAFORGE_URL" $PublicUrl
Set-EnvKey "LIBRAFORGE_INTERNAL_URL" $InternalUrl
Write-Host "LIBRAFORGE_URL / LIBRAFORGE_INTERNAL_URL configured"
