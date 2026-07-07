# Build a signed Play Store bundle (AAB).
# Requires: Android Studio JBR, keystore in frontend/android/

$ErrorActionPreference = "Stop"
$frontend = Split-Path $PSScriptRoot -Parent
$android = Join-Path $frontend "android"

$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"

Push-Location $frontend
try {
  npm run android:icons
  npm run build
  npx cap sync android
} finally {
  Pop-Location
}

Push-Location $android
try {
  .\gradlew bundleRelease assembleRelease
  $aab = Join-Path $android "app\build\outputs\bundle\release\app-release.aab"
  $apk = Join-Path $android "app\build\outputs\apk\release\app-release.apk"
  if (Test-Path $apk) {
    Write-Host ""
    Write-Host "Signed APK ready (sideload / Android Auto):" -ForegroundColor Green
    Write-Host $apk
  }
  if (Test-Path $aab) {
    Write-Host ""
    Write-Host "Signed AAB ready (Play Store):" -ForegroundColor Green
    Write-Host $aab
  }
} finally {
  Pop-Location
}
