<#
.SYNOPSIS  Keep the public server up on this laptop (run at logon by the HealthPortalWatchdog scheduled task).
.DESCRIPTION
  * Nothing running yet (fresh logon)      -> scripts\start_public.ps1 (tunnel + app)
  * App stops answering /healthz 3x in a row -> scripts\start_public.ps1 -AppOnly (tunnel and URL kept)
  * Tunnel process gone                     -> scripts\start_public.ps1 (a quick tunnel gets a NEW URL;
                                               CF_TUNNEL_TOKEN / ngrok keep the same one)
  Only one watchdog runs at a time (named mutex). Log: data\logs\watchdog.log (rotated at 1 MB).
  Stop it: Stop-ScheduledTask HealthPortalWatchdog   (or end the powershell process running watchdog.ps1)
.EXAMPLE  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog.ps1
#>
param(
    [int]$Port = 8000,
    [int]$IntervalSeconds = 30
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$state = Join-Path $root "data\public"
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $state, $logDir | Out-Null
$logFile = Join-Path $logDir "watchdog.log"
$starter = Join-Path $PSScriptRoot "start_public.ps1"

$mutex = New-Object System.Threading.Mutex($false, "Local\HealthPortalWatchdog")
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log([string]$msg) {
    if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt 1MB) { Move-Item -Force $logFile "$logFile.1" }
    "$(Get-Date -Format o) $msg" | Add-Content $logFile
}

function Alive([string]$pidFile, [string]$pattern) {
    if (-not (Test-Path $pidFile)) { return $false }
    $id = [int](Get-Content $pidFile -Raw)
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
    return [bool]($p -and $p.CommandLine -match $pattern)
}

$curl = Join-Path $env:SystemRoot "System32\curl.exe"
$script:why = ""
function Healthy {
    # a fresh curl.exe per probe: Invoke-WebRequest inside this long-lived hidden process produced false failures
    if (Test-Path $curl) {
        $code = & $curl -s -o NUL -w "%{http_code}" --max-time 8 "http://127.0.0.1:$Port/healthz" 2>$null
        $script:why = "http=$code curl_exit=$LASTEXITCODE"
        return $code -eq "200"
    }
    try {
        $req = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/healthz")
        $req.KeepAlive = $false; $req.Timeout = 8000; $req.Proxy = $null
        $resp = $req.GetResponse(); $ok = [int]$resp.StatusCode -eq 200; $resp.Close()
        return $ok
    } catch { $script:why = $_.Exception.Message; return $false }
}

function Run-Starter([string[]]$extra) {
    Log "running start_public.ps1 $($extra -join ' ')"
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $starter -Port $Port @extra 2>&1
    $out | ForEach-Object { Log "  $_" }
}

Log "watchdog started (pid $PID)"
$fails = 0
while ($true) {
    $tunnel = Alive (Join-Path $state "cloudflared.pid") "cloudflared|ngrok"
    $ok = Healthy
    if (-not $tunnel) {
        Log "tunnel not running -> full start"
        Run-Starter @()
        $fails = 0
    } elseif ($ok) {
        $fails = 0
    } else {
        $fails++
        Log "healthz failed ($fails/3) $($script:why)"
        if ($fails -ge 3) {
            Run-Starter @("-AppOnly")
            $fails = 0
        }
    }
    Start-Sleep $IntervalSeconds
}
