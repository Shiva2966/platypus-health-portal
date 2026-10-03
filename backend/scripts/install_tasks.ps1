<#
.SYNOPSIS  Create/refresh the per-user scheduled tasks (no administrator rights needed).
  HealthPortalWatchdog  at logon            -> scripts\watchdog.ps1 (starts tunnel + app, restarts the app if it dies)
  HealthPortalBackup    every 6 h + at logon -> scripts\backup.ps1 -VerifyRestore -Log (28 kept = 7 days;
                                                encrypted off-site copy when BACKUP_OFFSITE_DIR is set in .env)
.EXAMPLE  powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
.EXAMPLE  powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove
Inspect:  Get-ScheduledTask HealthPortal* | Get-ScheduledTaskInfo
#>
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"
$names = "HealthPortalWatchdog", "HealthPortalBackup"

foreach ($n in $names) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $n -Confirm:$false }
}
if ($Remove) { Write-Host "Removed: $($names -join ', ')"; exit 0 }

$ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# --- watchdog: at logon, runs forever, restarted by Task Scheduler if it ever exits with an error
$wdAction = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $root `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\watchdog.ps1`""
$wdTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$wdTrigger.Delay = "PT30S"
$wdSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "HealthPortalWatchdog" -Action $wdAction -Trigger $wdTrigger -Principal $principal `
    -Settings $wdSettings -Description "Health portal: start the public tunnel + app at logon and restart the app if it dies." | Out-Null

# --- backup: every 6 hours (repeating daily trigger) + 10 min after logon
$bkAction = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $root `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\backup.ps1`" -VerifyRestore -Log"
$bkRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours((Get-Date).Hour + 1) `
    -RepetitionInterval (New-TimeSpan -Hours 6)
$bkLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$bkLogon.Delay = "PT10M"
$bkSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "HealthPortalBackup" -Action $bkAction -Trigger @($bkRepeat, $bkLogon) -Principal $principal `
    -Settings $bkSettings -Description "Health portal: verified database backup every 6 hours and at logon (scripts\backup.py)." | Out-Null

Get-ScheduledTask -TaskName $names | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    Write-Host ("{0,-22} state={1,-8} next={2}" -f $_.TaskName, $_.State, $i.NextRunTime)
}
