# Decide whether a new Android APK GitHub Release is needed, bump versions,
# commit/push, and push an android-v{name}+{code} tag (triggers Actions).
#
# Called from deploy.ps1. Can also run alone:
#   .\scripts\release_android_apk.ps1
#   .\scripts\release_android_apk.ps1 -Force
#   .\scripts\release_android_apk.ps1 -DryRun

param(
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Skip
)

$ErrorActionPreference = "Continue"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$RepoRoot = Split-Path $PSScriptRoot -Parent
$BuildGradle = Join-Path $RepoRoot "frontend\android\app\build.gradle"

# Paths whose changes require a new sideloaded APK (Capacitor bundles the SPA).
$ApkRelevantPatterns = @(
    "^frontend/src/",
    "^frontend/android/",
    "^frontend/public/",
    "^frontend/capacitor\.config\.ts$",
    "^frontend/package\.json$",
    "^frontend/package-lock\.json$",
    "^\.github/workflows/android-apk-release\.yml$"
)

function Write-ApkStep([string]$Message) {
    Write-Host $Message -ForegroundColor Cyan
}

function Invoke-Git([string[]]$Arguments, [string]$FailMessage = "git failed") {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $output = & git -C $RepoRoot @Arguments 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $text = @($output | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
    }) -join "`n"
    if ($code -ne 0) {
        throw "$FailMessage (exit $code): $text"
    }
    return $text
}

function Get-LastAndroidTag {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $tags = & git -C $RepoRoot tag -l "android-v*" --sort=-creatordate 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($code -ne 0 -or -not $tags) { return $null }
    $first = @($tags | Where-Object { $_ -and "$_".Trim() } | ForEach-Object { "$_".Trim() }) | Select-Object -First 1
    return $first
}

function Parse-AndroidTag([string]$Tag) {
    # android-v1.5+6  or  android-v1.5
    if ($Tag -match '^android-v([^+]+)\+(\d+)$') {
        return @{ VersionName = $Matches[1]; VersionCode = [int]$Matches[2]; Tag = $Tag }
    }
    if ($Tag -match '^android-v(.+)$') {
        return @{ VersionName = $Matches[1]; VersionCode = $null; Tag = $Tag }
    }
    return $null
}

function Get-LocalAndroidVersion {
    $raw = Get-Content -Raw $BuildGradle
    if ($raw -notmatch 'versionCode\s+(\d+)') { throw "versionCode not found in build.gradle" }
    $code = [int]$Matches[1]
    if ($raw -notmatch 'versionName\s+"([^"]+)"') { throw "versionName not found in build.gradle" }
    return @{ VersionCode = $code; VersionName = $Matches[1] }
}

function Set-LocalAndroidVersion([int]$VersionCode, [string]$VersionName) {
    $raw = Get-Content -Raw $BuildGradle
    $raw = [regex]::Replace($raw, 'versionCode\s+\d+', "versionCode $VersionCode")
    $raw = [regex]::Replace($raw, 'versionName\s+"[^"]*"', "versionName `"$VersionName`"")
    Set-Content -Path $BuildGradle -Value $raw -NoNewline
}

function Get-NextVersionName([string]$Current) {
    $parts = $Current.Split(".")
    if ($parts.Count -eq 0) { return "1.0" }
    $last = 0
    if (-not [int]::TryParse($parts[-1], [ref]$last)) {
        return "$Current.1"
    }
    $parts[-1] = [string]($last + 1)
    return ($parts -join ".")
}

function Test-ApkReleaseNeeded([string]$LastTag) {
    if ($Force) { return $true }
    if (-not $LastTag) {
        Write-Host '  No prior android-v* tag - APK release needed.' -ForegroundColor Yellow
        return $true
    }

    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $changed = & git -C $RepoRoot diff --name-only "$LastTag..HEAD" 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($code -ne 0) {
        Write-Host "  Could not diff against $LastTag - assuming APK release needed." -ForegroundColor Yellow
        return $true
    }

    $files = @($changed | ForEach-Object { "$_".Trim().Replace("\", "/") } | Where-Object { $_ })
    $hits = @()
    foreach ($f in $files) {
        foreach ($pat in $ApkRelevantPatterns) {
            if ($f -match $pat) {
                $hits += $f
                break
            }
        }
    }

    if ($hits.Count -eq 0) {
        Write-Host "  No APK-relevant changes since $LastTag - skipping APK release." -ForegroundColor DarkGray
        return $false
    }

    $hitCount = $hits.Count
    Write-Host ('  APK-relevant changes since {0} - {1} files:' -f $LastTag, $hitCount) -ForegroundColor Yellow
    $hits | Select-Object -First 12 | ForEach-Object { Write-Host "    - $_" }
    if ($hitCount -gt 12) {
        Write-Host ('    ... and {0} more' -f ($hitCount - 12))
    }
    return $true
}

# ---------------------------------------------------------------------------

if ($Skip) {
    Write-Host 'APK: Skipping Android release (-SkipApk).' -ForegroundColor Yellow
    exit 0
}

Write-ApkStep 'APK: Checking whether a new Android APK release is needed'

$branch = (Invoke-Git @("rev-parse", "--abbrev-ref", "HEAD") "rev-parse failed").Trim()
if ($branch -ne "main" -and -not $Force) {
    Write-Host "  On branch '$branch' (not main) - skipping APK release (use -ForceApk to override)." -ForegroundColor Yellow
    exit 0
}

$status = (Invoke-Git @("status", "--porcelain") "status failed").Trim()
if ($status -and -not $DryRun) {
    Write-Host '  Working tree is dirty - skipping APK release so we do not mix uncommitted work into a tag.' -ForegroundColor Yellow
    Write-Host '  Commit/push your changes (or stash), then re-run with -ForceApk, or run:' -ForegroundColor DarkGray
    Write-Host '    .\scripts\release_android_apk.ps1 -Force' -ForegroundColor DarkGray
    exit 0
}

$lastTag = Get-LastAndroidTag
$needed = Test-ApkReleaseNeeded $lastTag
if (-not $needed) { exit 0 }

$local = Get-LocalAndroidVersion
$lastParsed = if ($lastTag) { Parse-AndroidTag $lastTag } else { $null }

$baseCode = $local.VersionCode
if ($lastParsed -and $null -ne $lastParsed.VersionCode -and $lastParsed.VersionCode -gt $baseCode) {
    $baseCode = $lastParsed.VersionCode
}
$nextCode = $baseCode + 1
$nextName = Get-NextVersionName $local.VersionName

$tag = "android-v${nextName}+${nextCode}"
Write-Host "  Next release: v$nextName (build $nextCode) -> tag $tag" -ForegroundColor Green

if ($DryRun) {
    Write-Host '  Dry run - not committing or pushing.' -ForegroundColor Yellow
    exit 0
}

try {
    Invoke-Git @("fetch", "origin", "--tags", "--quiet") "git fetch failed" | Out-Null
}
catch {
    Write-Host "  Warning: git fetch --tags failed: $($_.Exception.Message)" -ForegroundColor Yellow
}

$existing = Invoke-Git @("tag", "-l", $tag) "tag list failed"
if ($existing.Trim()) {
    throw "Tag $tag already exists. Bump versionCode manually or delete the tag."
}

Set-LocalAndroidVersion -VersionCode $nextCode -VersionName $nextName
Write-Host "  Updated frontend/android/app/build.gradle -> versionName $nextName / versionCode $nextCode"

Invoke-Git @("add", "frontend/android/app/build.gradle") "git add failed" | Out-Null
$commitMsg = "Bump Android app to $nextName ($nextCode)."
Invoke-Git @("commit", "-m", $commitMsg) "git commit failed" | Out-Null
Write-Host '  Committed version bump.'

Invoke-Git @("push", "-u", "origin", "HEAD") "git push failed" | Out-Null
Write-Host '  Pushed version bump to origin.'

Invoke-Git @("tag", "-a", $tag, "-m", "Library Android $nextName (build $nextCode)") "git tag failed" | Out-Null
Invoke-Git @("push", "origin", $tag) "git push tag failed" | Out-Null
Write-Host "  Pushed tag $tag - GitHub Actions will build and publish the APK." -ForegroundColor Green
Write-Host '  Watch: https://github.com/brutaliccus/Library/actions/workflows/android-apk-release.yml' -ForegroundColor Cyan
exit 0
