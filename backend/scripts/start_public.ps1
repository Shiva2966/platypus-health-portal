<#
.SYNOPSIS  Start the production server on 127.0.0.1:<Port> behind a public HTTPS tunnel.
.DESCRIPTION
  1. stops the server/tunnel this script started last time (pid files in data\public\)
  2. starts the tunnel and determines the public URL. Tunnel choice (-Tunnel auto picks the first that is configured;
     values come from the environment or .env, and are never printed):
       cloudflare  CF_TUNNEL_TOKEN + PUBLIC_URL   named Cloudflare Tunnel on your own domain (permanent URL)
       ngrok       NGROK_AUTHTOKEN + NGROK_DOMAIN free ngrok static domain (permanent URL)
       quick       (default)                      https://<random>.trycloudflare.com (changes on every start)
  3. sets ALLOWED_ORIGINS in .env (only that key) to the public URL + the Capacitor app origins
  4. starts uvicorn with --proxy-headers so request.scheme is https (Secure cookies + HSTS), AUTO_MIGRATE=1
  5. waits for /readyz, writes the URL to health-portal-clients\config\server.json (+ the clients' bundled copies)
  -AppOnly restarts ONLY uvicorn (tunnel, URL, .env and client config untouched). Permanent URL: docs\PUBLIC_ACCESS.md.
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -AppOnly
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -Stop
#>
param(
    [int]$Port = 8000,
    [string]$ClientsDir = "C:\Users\sav11\Projects\health-portal-clients",
    [ValidateSet("auto", "quick", "cloudflare", "ngrok")][string]$Tunnel = "auto",
    [switch]$AppOnly,
    [switch]$Stop
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$state = Join-Path $root "data\public"
New-Item -ItemType Directory -Force -Path $state | Out-Null
$uvPid = Join-Path $state "uvicorn.pid"
$cfPid = Join-Path $state "cloudflared.pid"   # pid of whichever tunnel program runs (name kept for compatibility)
$urlFile = Join-Path $state "public_url.txt"
$cfLog = Join-Path $state "cloudflared.log"
$uvLog = Join-Path $state "uvicorn.log"
$uvErr = Join-Path $state "uvicorn.err.log"

function Stop-Tree([int]$id) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Tree $_.ProcessId }
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}

function Stop-FromPidFile([string]$f) {
    if (Test-Path $f) {
        $id = [int](Get-Content $f -Raw)
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        if ($p -and $p.CommandLine -match "uvicorn|cloudflared|ngrok") { Stop-Tree $id; Write-Host "stopped pid $id" }
        Remove-Item $f -Force
    }
}

function Stop-Previous { foreach ($f in $uvPid, $cfPid) { Stop-FromPidFile $f } }

function Write-Utf8NoBom([string]$path, [string]$text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false)))
}

function Get-Setting([string]$name) {
    # process environment wins; otherwise the KEY=value line in .env. Never printed.
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { return $v.Trim() }
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        foreach ($line in [IO.File]::ReadAllLines($envFile)) {
            if ($line -match "^\s*$([regex]::Escape($name))\s*=(.*)$") { return $Matches[1].Trim().Trim('"').Trim("'") }
        }
    }
    return ""
}

function Rotate-Log([string]$path, [int]$keep = 3) {
    if (-not (Test-Path $path) -or (Get-Item $path).Length -eq 0) { return }
    for ($i = $keep - 1; $i -ge 1; $i--) {
        if (Test-Path "$path.$i") { Move-Item -Force "$path.$i" "$path.$($i + 1)" }
    }
    Move-Item -Force $path "$path.1"
}

function Start-App {
    $busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($busy) { throw "Port $Port is already in use by pid $($busy[0].OwningProcess). Stop it first." }
    Rotate-Log $uvErr
    Rotate-Log $uvLog
    # loopback only; trust X-Forwarded-* from the local tunnel process only
    $env:AUTO_MIGRATE = "1"
    $s = Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port",
                        "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--no-access-log") `
        -RedirectStandardOutput $uvLog -RedirectStandardError $uvErr
    Remove-Item Env:AUTO_MIGRATE
    Set-Content $uvPid $s.Id
    $ready = $false
    for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
        Start-Sleep 1
        try { $ready = (Invoke-WebRequest "http://127.0.0.1:$Port/readyz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch {}
        if ($s.HasExited) { break }
    }
    if (-not $ready) { Write-Warning "Server not ready on :$Port yet (see $uvErr). /readyz returns 503 if email or migrations are not OK." }
    return $ready
}

# ---------------------------------------------------------------- -Stop / -AppOnly
if ($Stop) { Stop-Previous; Write-Host "Stopped."; exit 0 }

if ($AppOnly) {
    Stop-FromPidFile $uvPid
    for ($i = 0; $i -lt 15 -and (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
    $null = Start-App
    $url = if (Test-Path $urlFile) { (Get-Content $urlFile -Raw).Trim() } else { "(unknown - tunnel not started by this script)" }
    Write-Host "app restarted (tunnel untouched). PUBLIC URL: $url/"
    exit 0
}

Stop-Previous
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($busy) { throw "Port $Port is already in use by pid $($busy[0].OwningProcess). Stop it first." }

# ---------------------------------------------------------------- 1. tunnel
$cfToken = Get-Setting "CF_TUNNEL_TOKEN"
$ngToken = Get-Setting "NGROK_AUTHTOKEN"
$ngDomain = (Get-Setting "NGROK_DOMAIN") -replace "^https?://", "" -replace "/+$", ""
if ($Tunnel -eq "auto") {
    $Tunnel = if ($cfToken) { "cloudflare" } elseif ($ngToken -and $ngDomain) { "ngrok" } else { "quick" }
}

function Find-Exe([string]$name, [string[]]$extra) {
    $c = (Get-Command $name -ErrorAction SilentlyContinue).Source
    if ($c) { return $c }
    return $extra | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

Remove-Item $cfLog -ErrorAction SilentlyContinue
$url = $null
if ($Tunnel -eq "ngrok") {
    if (-not ($ngToken -and $ngDomain)) { throw "ngrok needs NGROK_AUTHTOKEN and NGROK_DOMAIN (.env)" }
    $ng = Find-Exe "ngrok" @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\ngrok.exe", "$env:ProgramFiles\ngrok\ngrok.exe")
    if (-not $ng) { throw "ngrok not found. winget install --id Ngrok.Ngrok -e" }
    $env:NGROK_AUTHTOKEN = $ngToken   # read by the agent from its environment, never on the command line
    $t = Start-Process -FilePath $ng -ArgumentList @("http", "127.0.0.1:$Port", "--domain=$ngDomain", "--log=stdout") `
        -RedirectStandardOutput $cfLog -RedirectStandardError (Join-Path $state "cloudflared.out.log") -WindowStyle Hidden -PassThru
    Remove-Item Env:NGROK_AUTHTOKEN
    $url = "https://$ngDomain"
} else {
    $cf = Find-Exe "cloudflared" @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe")
    if (-not $cf) { throw "cloudflared not found. winget install --id Cloudflare.cloudflared -e" }
    if ($Tunnel -eq "cloudflare") {
        if (-not $cfToken) { throw "named Cloudflare Tunnel needs CF_TUNNEL_TOKEN (.env)" }
        $url = (Get-Setting "PUBLIC_URL").TrimEnd("/")
        if (-not $url) { throw "set PUBLIC_URL (e.g. https://hp.example.com) for the named Cloudflare Tunnel" }
        $env:TUNNEL_TOKEN = $cfToken   # cloudflared reads TUNNEL_TOKEN; keeps it out of the process list
        $t = Start-Process -FilePath $cf -ArgumentList @("tunnel", "--no-autoupdate", "run") `
            -RedirectStandardError $cfLog -RedirectStandardOutput (Join-Path $state "cloudflared.out.log") -WindowStyle Hidden -PassThru
        Remove-Item Env:TUNNEL_TOKEN
    } else {
        $t = Start-Process -FilePath $cf -ArgumentList @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$Port") `
            -RedirectStandardError $cfLog -RedirectStandardOutput (Join-Path $state "cloudflared.out.log") -WindowStyle Hidden -PassThru
    }
}
Set-Content $cfPid $t.Id
if ($Tunnel -eq "quick") {
    for ($i = 0; $i -lt 90 -and -not $url; $i++) {
        Start-Sleep 1
        if (Test-Path $cfLog) {
            $m = Select-String -Path $cfLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value }
        }
        if ($t.HasExited) { break }
    }
} else {
    Start-Sleep 3
    if ($t.HasExited) { $url = $null }
}
if (-not $url) { Stop-Previous; throw "Tunnel ($Tunnel) did not start. See $cfLog" }
Write-Host "tunnel ($Tunnel): $url"
Write-Utf8NoBom $urlFile $url

# ---------------------------------------------------------------- 2. CORS / CSRF allow-list (only this key in .env changes)
& $py scripts\set_env.py "ALLOWED_ORIGINS=$url,https://localhost,capacitor://localhost"
if ($LASTEXITCODE -ne 0) { Stop-Previous; throw "Could not update ALLOWED_ORIGINS in .env" }

# ---------------------------------------------------------------- 3. app server
$null = Start-App

# ---------------------------------------------------------------- 4. client default URL
$cfg = Join-Path $ClientsDir "config\server.json"
if (Test-Path (Split-Path $cfg)) {
    $json = [ordered]@{
        serverUrl = $url
        _comment  = "Written by health-portal\scripts\start_public.ps1 ($Tunnel tunnel). Used as the DEFAULT by both clients; each client can still change it in its own settings screen."
    } | ConvertTo-Json
    Write-Utf8NoBom $cfg ($json + "`n")
    Write-Host "wrote $cfg"
    if (Get-Command node -ErrorAction SilentlyContinue) {
        foreach ($app in "desktop-app", "android-app") {
            $sync = Join-Path $ClientsDir "$app\scripts\sync-config.js"
            if (Test-Path $sync) { & node $sync | Out-Null }
        }
    }
}

Write-Host ""
Write-Host "PUBLIC URL (patients):  $url/"
Write-Host "STAFF PORTAL:           $url/staff/"
Write-Host "Logs: $state   App only: -AppOnly   Stop: powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -Stop"
