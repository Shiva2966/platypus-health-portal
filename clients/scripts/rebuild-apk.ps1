# One-click: re-sync the current server URL and rebuild the patient APK.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location "$root\android-app"
npm run sync
Set-Location android
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat assembleDebug
Copy-Item app\build\outputs\apk\debug\app-debug.apk "$root\HealthPortal-patient-debug.apk" -Force
Write-Host "Done: $root\HealthPortal-patient-debug.apk"
