<#
.SYNOPSIS  One-time setup: permanent URL (free ngrok static domain), no sleep, off-site backups, auto-start, rebuild APK.
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\make_permanent.ps1
 Before running: sign up free at https://dashboard.ngrok.com, copy (a) Your Authtoken (Getting Started > Your Authtoken)
 and (b) your free static domain (Universe/Domains, looks like  something-something.ngrok-free.app).
 Secrets are typed here and go only into .env (never printed, never sent anywhere else).
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

# 1. ngrok installed?
if (-not (Get-Command ngrok -ErrorAction SilentlyContinue) -and -not (Test-Path "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ngrok.exe")) {
    Write-Host "Installing ngrok..."; winget install --id Ngrok.Ngrok -e --accept-source-agreements --accept-package-agreements
}

# 2. permanent URL settings (into .env only)
$tok = Read-Host "ngrok Authtoken"
$dom = (Read-Host "ngrok static domain (e.g. name.ngrok-free.app)") -replace "^https?://", "" -replace "/+$", ""
if (-not $tok -or -not $dom) { throw "Both values are required." }
& $py scripts\set_env.py "NGROK_AUTHTOKEN=$tok" "NGROK_DOMAIN=$dom" "PUBLIC_URL=https://$dom"
if ($LASTEXITCODE -ne 0) { throw "Could not write .env" }

# 3. laptop must never sleep while plugged in; screen may turn off
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10
Write-Host "Sleep disabled while plugged in (keep the charger connected)."

# 4. encrypted off-site backups (OneDrive sync folder)
$off = Join-Path $env:USERPROFILE "OneDrive\HealthPortalBackups"
New-Item -ItemType Directory -Force -Path $off | Out-Null
& $py scripts\set_env.py "BACKUP_OFFSITE_DIR=$off"

# 5. scheduled tasks (backup every 6h + watchdog at logon), then start with the permanent URL
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -Tunnel ngrok

# 6. health check + rebuild the APK with the permanent URL baked in
Start-Sleep 5
$u = "https://$dom"
$code = (curl.exe -s -o NUL -w "%{http_code}" "$u/healthz")
Write-Host "Public health check $u/healthz -> $code (200 = good)"
powershell -ExecutionPolicy Bypass -File "$root\..\health-portal-clients\scripts\rebuild-apk.ps1"
Write-Host "`nALL DONE. Permanent address: $u/   Staff: $u/staff/"
