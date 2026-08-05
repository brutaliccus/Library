param(
    [string]$RepoRoot = "",
    [string]$Dest = "",
    [switch]$Force
)
$ErrorActionPreference = "Continue"
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "python required for OL catalog fetch"
    exit 1
}
$argsList = @((Join-Path $RepoRoot "scripts\fetch_ol_catalog.py"))
if ($Dest) { $argsList += @("--dest", $Dest) }
if ($Force) { $argsList += "--force" }
& $py.Source @argsList
exit $LASTEXITCODE
