# Pre-deploy check suite: backend tests, frontend type-check, Android compile.
# Run from anywhere: .\scripts\check.ps1
# Skip the slow gradle step: .\scripts\check.ps1 -SkipAndroid

param(
    [switch]$SkipAndroid
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$failed = @()

function Run-Check {
    param([string]$Name, [string]$Dir, [scriptblock]$Cmd)
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    Push-Location $Dir
    try {
        & $Cmd
        if ($LASTEXITCODE -ne 0) {
            $script:failed += $Name
            Write-Host "FAILED: $Name" -ForegroundColor Red
        } else {
            Write-Host "OK: $Name" -ForegroundColor Green
        }
    } finally {
        Pop-Location
    }
}

Run-Check "Backend tests (pytest)" $root {
    if (-not (Test-Path (Join-Path $root "tests"))) {
        Write-Host "No tests/ directory — skipping pytest." -ForegroundColor Yellow
        $global:LASTEXITCODE = 0
        return
    }
    python -m pytest tests -q
    # pytest exit 5 = no tests collected
    if ($LASTEXITCODE -eq 5) { $global:LASTEXITCODE = 0 }
}
Run-Check "Frontend type-check (tsc)" (Join-Path $root "frontend") { npx tsc --noEmit }

if (-not $SkipAndroid) {
    Run-Check "Android compile (gradle)" (Join-Path $root "frontend\android") {
        .\gradlew.bat compileDebugJavaWithJavac --console=plain -q
    }
} else {
    Write-Host "`n(skipping Android compile)" -ForegroundColor Yellow
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "Checks failed: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "All checks passed." -ForegroundColor Green
exit 0
