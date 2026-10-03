<#
.SYNOPSIS  Idempotent launcher called by the HealthPortalAutoStart scheduled task.
.DESCRIPTION
  Called at log-on and on resume from sleep (Power-Troubleshooter EventID 1).
  * Both uvicorn (:8000/healthz) and tunnel process already healthy → exits immediately (no-op).
  * Services not healthy → waits up to NetworkWaitSeconds for the network stack to come up
    (important after resume from sleep), then runs start_public.ps1 for a full start.
  * Ensures HealthPortalWatchdog task is running after a successful start.
#>
param(
    [int]$Port = 8000,
    [int]$NetworkWaitSeconds = 20
)
$ErrorActionPreference = "Continue"
$root   = Split-Path -Parent $PSScriptRoot
Set-Location $root
$state  = Join-Path $root "data\public"
$uvPid  = Join-Path $state "uvicorn.pid"
$cfPid  = Join-Path $state "cloudflared.pid"

function Alive([string]$pidFile, [string]$pattern) {
    if (-not (Test-Path $pidFile)) { return $false }
    $id = try { [int](Get-Content $pidFile -Raw -ErrorAction Stop) } catch { return $false }
    $p  = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
    return [bool]($p -and $p.CommandLine -match $pattern)
}

function HealthOK {
    $curl = "$env:SystemRoot\System32\curl.exe"
    if (Test-Path $curl) {
        $code = & $curl -s -o NUL -w "%{http_code}" --max-time 5 "http://127.0.0.1:$Port/healthz" 2>$null
        return $code -eq "200"
    }
    try {
        $req = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/healthz")
        $req.KeepAlive = $false; $req.Timeout = 5000; $req.Proxy = $null
        $resp = $req.GetResponse()
        $ok   = [int]$resp.StatusCode -eq 200
        $resp.Close()
        return $ok
    } catch { return $false }
}

# Fast path: both app and tunnel are up → nothing to do
if ((HealthOK) -and (Alive $cfPid "cloudflared|ngrok")) {
    exit 0
}

# Services are down (or stale) – wait for network before attempting start
$deadline = (Get-Date).AddSeconds($NetworkWaitSeconds)
while ((Get-Date) -lt $deadline) {
    if ([Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable()) { break }
    Start-Sleep 2
}
Start-Sleep 2   # brief settle time so DNS / routing are stable

# Re-check after network wait (watchdog may have already restarted things)
if ((HealthOK) -and (Alive $cfPid "cloudflared|ngrok")) {
    exit 0
}

# Full start: kills any stale processes and starts tunnel + app
& powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
    -File (Join-Path $PSScriptRoot "start_public.ps1")
$rc = $LASTEXITCODE

# Ensure HealthPortalWatchdog is running (no-op if already running; its internal mutex prevents duplicates)
$wdTask = Get-ScheduledTask -TaskName "HealthPortalWatchdog" -ErrorAction SilentlyContinue
if ($wdTask -and $wdTask.State -ne "Running") {
    Start-ScheduledTask -TaskName "HealthPortalWatchdog" -ErrorAction SilentlyContinue
}

exit $rc
