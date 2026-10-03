<#
.SYNOPSIS  Run the FULL test suite on SQLite, then on PostgreSQL (embedded via pgserver, no Docker needed).
.EXAMPLE   .\scripts\test_all.ps1                 # both engines
.EXAMPLE   .\scripts\test_all.ps1 -Engine postgres -Pytest "-x -q tests/test_db_*.py"
Set HP_TEST_DATABASE_URL yourself to point at any PostgreSQL (e.g. the docker-compose db) instead.
#>
param(
    [ValidateSet("both", "sqlite", "postgres")][string]$Engine = "both",
    [string]$Pytest = "-q"
)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$rc = 0
if ($Engine -in "both", "sqlite") {
    Write-Host "=== SQLite ===" -ForegroundColor Cyan
    Remove-Item Env:HP_TEST_DATABASE_URL -ErrorAction SilentlyContinue
    Invoke-Expression "& `"$py`" -m pytest $Pytest"
    if ($LASTEXITCODE -ne 0) { $rc = 1 }
}
if ($Engine -in "both", "postgres") {
    Write-Host "=== PostgreSQL ===" -ForegroundColor Cyan
    if (-not $env:HP_TEST_DATABASE_URL) {
        $url = (& $py scripts\pg_dev.py newdb 2>$null | Select-Object -Last 1)
        if (-not $url -or $url -notmatch "^postgresql") {
            Write-Host "Could not start embedded PostgreSQL (pip install -r requirements-pgtest.txt) - or set HP_TEST_DATABASE_URL." -ForegroundColor Red
            exit 1
        }
        $env:HP_TEST_DATABASE_URL = $url
    }
    $env:HP_TEST_DATABASE_URL
    Invoke-Expression "& `"$py`" -m pytest $Pytest"
    if ($LASTEXITCODE -ne 0) { $rc = 1 }
    Remove-Item Env:HP_TEST_DATABASE_URL -ErrorAction SilentlyContinue
}
exit $rc
