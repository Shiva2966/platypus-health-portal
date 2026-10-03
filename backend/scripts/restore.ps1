<#
.SYNOPSIS  Restore a backup into an EMPTY database and verify it (wraps scripts/restore.py).
.EXAMPLE   .\scripts\restore.ps1 -Latest -Target sqlite:///data/restored.db
.EXAMPLE   .\scripts\restore.ps1 -Backup backups\healthportal_20261002T023000Z.pgdump -Target postgresql+psycopg://hp:pw@localhost:5432/hp_restored
#>
param(
    [string]$Backup = "",
    [switch]$Latest,
    [Parameter(Mandatory = $true)][string]$Target,
    [switch]$Force
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$args_ = @("scripts\restore.py", "--target", $Target)
if ($Latest) { $args_ += "--latest" } elseif ($Backup) { $args_ = @("scripts\restore.py", $Backup, "--target", $Target) }
if ($Force) { $args_ += "--force" }
& $py @args_
exit $LASTEXITCODE
