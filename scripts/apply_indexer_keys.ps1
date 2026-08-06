# Push Jackett/Prowlarr URL+API keys from .env into the running Library app.
param(
    [string]$RepoRoot = ""
)
$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$envPath = if ($env:LIBRARY_ENV_FILE) { $env:LIBRARY_ENV_FILE } else { Join-Path $RepoRoot ".env" }
if (-not (Test-Path $envPath)) { Write-Error "no .env at $envPath"; exit 1 }

function Get-EnvKeyValue([string]$Path, [string]$Key) {
    $line = Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue |
        Where-Object { $_ -match "^${Key}=" } |
        Select-Object -First 1
    if (-not $line) { return "" }
    return $line.Substring($Key.Length + 1)
}
function Test-Placeholder([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    if ($Value -match '^[Yy]our-') { return $true }
    if ($Value -eq "changeme") { return $true }
    return $false
}

$ju = Get-EnvKeyValue $envPath "JACKETT_URL"
if (-not $ju) { $ju = "http://audiobook-jackett:9117" }
$jk = Get-EnvKeyValue $envPath "JACKETT_API_KEY"
$pu = Get-EnvKeyValue $envPath "PROWLARR_URL"
if (-not $pu) { $pu = "http://prowlarr:9696" }
$pk = Get-EnvKeyValue $envPath "PROWLARR_API_KEY"

$missing = $false
if (Test-Placeholder $jk) {
    Write-Host "error: JACKETT_API_KEY missing — run .\scripts\configure_jackett.ps1 -ForceBundled" -ForegroundColor Red
    $missing = $true
}
if (Test-Placeholder $pk) {
    Write-Host "error: PROWLARR_API_KEY missing — run .\scripts\configure_prowlarr.ps1 -ForceBundled" -ForegroundColor Red
    $missing = $true
}
if ($missing) { exit 1 }

Set-Location $RepoRoot
Write-Host "Recreating app so Admin Overview picks up Jackett/Prowlarr keys from .env ..."
docker compose up -d --force-recreate --no-deps app
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Seeding config.jackett_* / config.prowlarr_* into app_settings ..."
$ready = $false
for ($i = 1; $i -le 30; $i++) {
    docker compose exec -T app true 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host "warn: app not ready for exec — keys are in .env; recreate succeeded" -ForegroundColor Yellow
    exit 0
}

$py = @"
import asyncio, os
async def main():
    from app.services import app_settings
    from app.services.instance_settings import apply_runtime_overrides, invalidate_cache
    pairs = [
        ("config.jackett_url", os.environ.get("JACKETT_URL_SEED", "").strip()),
        ("config.jackett_api_key", os.environ.get("JACKETT_API_KEY_SEED", "").strip()),
        ("config.prowlarr_url", os.environ.get("PROWLARR_URL_SEED", "").strip()),
        ("config.prowlarr_api_key", os.environ.get("PROWLARR_API_KEY_SEED", "").strip()),
    ]
    for key, val in pairs:
        if val and not val.lower().startswith("your-"):
            await app_settings.set_setting(key, val)
            print(f"seeded {key}")
    invalidate_cache()
    await apply_runtime_overrides()
    print("apply_runtime_overrides done")
asyncio.run(main())
"@
$env:JACKETT_URL_SEED = $ju
$env:JACKETT_API_KEY_SEED = $jk
$env:PROWLARR_URL_SEED = $pu
$env:PROWLARR_API_KEY_SEED = $pk
$py | docker compose exec -T `
    -e JACKETT_URL_SEED `
    -e JACKETT_API_KEY_SEED `
    -e PROWLARR_URL_SEED `
    -e PROWLARR_API_KEY_SEED `
    app python -
Write-Host "Jackett/Prowlarr keys applied (.env + app_settings + app recreate)"
