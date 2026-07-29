# Harvest Android system crash evidence for the Android Auto "explorer restart" bug.
#
# Usage: connect the phone via USB with USB debugging enabled, then run:
#   powershell -ExecutionPolicy Bypass -File scripts\aa_crash_harvest.ps1
#
# Output goes to .\aa-crash-evidence\<timestamp>\ — attach/inspect from there.

$ErrorActionPreference = "Continue"

$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
if (-not (Test-Path $adb)) { $adb = "adb" }

$devices = & $adb devices | Select-Object -Skip 1 | Where-Object { $_ -match "\tdevice$" }
if (-not $devices) {
    Write-Host "No authorized device found. Plug the phone in, enable USB debugging, accept the prompt." -ForegroundColor Red
    & $adb devices -l
    exit 1
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path (Split-Path $PSScriptRoot -Parent) "aa-crash-evidence\$stamp"
New-Item -ItemType Directory -Force -Path $out | Out-Null
Write-Host "Collecting into $out"

# 1. Device identity — OEM matters for launcher/SystemUI behavior.
& $adb shell getprop ro.product.manufacturer *> "$out\device.txt"
& $adb shell getprop ro.product.model        *>> "$out\device.txt"
& $adb shell getprop ro.build.version.release *>> "$out\device.txt"
& $adb shell getprop ro.build.display.id      *>> "$out\device.txt"

# 2. Dropbox index — persistent record of every crash/ANR/watchdog for ~3 days.
& $adb shell dumpsys dropbox *> "$out\dropbox_index.txt"

# 3. Full text of recent crash-flavored dropbox entries.
#    These tags cover launcher crashes, SystemUI crashes, system_server death,
#    native tombstones, ANRs, and low-memory kills.
$tags = @(
    "system_app_crash", "system_app_anr", "system_app_native_crash",
    "data_app_crash", "data_app_anr", "data_app_native_crash",
    "system_server_crash", "system_server_watchdog", "system_server_lowmem",
    "SYSTEM_TOMBSTONE", "SYSTEM_LAST_KMSG", "SYSTEM_RESTART"
)
foreach ($tag in $tags) {
    & $adb shell dumpsys dropbox --print $tag *> "$out\dropbox_$tag.txt"
    if ((Get-Item "$out\dropbox_$tag.txt").Length -lt 64) {
        Remove-Item "$out\dropbox_$tag.txt"
    }
}

# 4. Logcat ring buffers (main/system/crash/events) — may still hold the last session.
& $adb logcat -d -b crash  -v threadtime *> "$out\logcat_crash.txt"
& $adb logcat -d -b system -v threadtime *> "$out\logcat_system.txt"
& $adb logcat -d -b events -v threadtime *> "$out\logcat_events.txt"
& $adb logcat -d -b main   -v threadtime *> "$out\logcat_main.txt"

# 5. Memory + media state snapshots.
& $adb shell dumpsys meminfo com.freiverse.library *> "$out\meminfo_app.txt"
& $adb shell dumpsys meminfo                       *> "$out\meminfo_all.txt"
& $adb shell dumpsys media_session                 *> "$out\media_session.txt"
& $adb shell dumpsys activity processes            *> "$out\activity_processes.txt"

Write-Host ""
Write-Host "Done. Evidence in: $out" -ForegroundColor Green
Write-Host "Quick scan of dropbox index for crash entries:"
Select-String -Path "$out\dropbox_index.txt" -Pattern "crash|anr|watchdog|lowmem|tombstone" |
    Select-Object -Last 40 | ForEach-Object { $_.Line }
