<#
.SYNOPSIS  Timestamped database backup with retention pruning (wraps scripts/backup.py).
.EXAMPLE   .\scripts\backup.ps1                      # SQLite or PostgreSQL, whatever DATABASE_URL says
.EXAMPLE   .\scripts\backup.ps1 -Keep 30 -Dest D:\hp-backups -VerifyRestore
.EXAMPLE   .\scripts\backup.ps1 -VerifyRestore -Log   # what the scheduled tasks run (scripts\install_tasks.ps1)
Off-site encrypted copy: set BACKUP_OFFSITE_DIR in .env (see docs\OPERATIONS.md).
#>
param(
    [string]$Dest = "",
    [int]$Keep = 28,
    [switch]$VerifyRestore,
    [switch]$Log
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$args_ = @("scripts\backup.py", "--keep", $Keep)
if ($Dest) { $args_ += @("--dest", $Dest) }
if ($VerifyRestore) { $args_ += "--verify-restore" }
if (-not $Log) {
    & $py @args_
    exit $LASTEXITCODE
}
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "backup.log"
if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt 1MB) {
    Move-Item -Force $logFile "$logFile.1"
}
$ErrorActionPreference = "Continue"
"=== $(Get-Date -Format o) backup start" | Add-Content $logFile
& $py @args_ *>&1 | ForEach-Object { "$_" } | Add-Content $logFile
$code = $LASTEXITCODE
"=== $(Get-Date -Format o) backup exit $code" | Add-Content $logFile
exit $code
