# Thin wrapper around configure_prowlarr.py
param(
    [string]$RepoRoot = "",
    [string]$ExternalUrl = "",
    [string]$ExternalApiKey = "",
    [switch]$ForceBundled
)
$ErrorActionPreference = "Continue"
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$env:LIBRARY_ENV_FILE = Join-Path $RepoRoot ".env"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "skip prowlarr configure (python required)"
    exit 0
}
$argsList = @((Join-Path $RepoRoot "scripts\configure_prowlarr.py"))
if ($ExternalUrl) { $argsList += @("--external-url", $ExternalUrl) }
if ($ExternalApiKey) { $argsList += @("--external-api-key", $ExternalApiKey) }
if ($ForceBundled) { $argsList += "--force-bundled" }
& $py.Source @argsList
exit $LASTEXITCODE
