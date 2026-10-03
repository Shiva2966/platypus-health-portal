<#
.SYNOPSIS  Alembic wrapper for Health Portal (uses .venv and DATABASE_URL).
.EXAMPLE   .\scripts\migrate.ps1 upgrade            # apply all migrations (default)
.EXAMPLE   .\scripts\migrate.ps1 new "add foo"      # autogenerate a new revision from the models
.EXAMPLE   .\scripts\migrate.ps1 check              # fail if models and migrations have drifted
.EXAMPLE   .\scripts\migrate.ps1 current|history|downgrade -1|stamp head
#>
param(
    [Parameter(Position = 0)][string]$Action = "upgrade",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

switch ($Action) {
    "upgrade"   { & $py -m alembic upgrade head }
    "new"       { & $py -m alembic revision --autogenerate -m ($Rest -join " ") }
    "check"     { & $py -m alembic check }
    "current"   { & $py -m alembic current }
    "history"   { & $py -m alembic history --verbose }
    "downgrade" { & $py -m alembic downgrade ($Rest -join " ") }
    "stamp"     { & $py -m alembic stamp ($Rest -join " ") }
    default     { & $py -m alembic $Action @Rest }
}
exit $LASTEXITCODE
