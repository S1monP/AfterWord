# Start AfterWord (scripts/run_home.ps1) whenever you log in to Windows,
# with no window. Run once:
#     powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1
# Stop it from starting:          ... scripts\autostart.ps1 -Remove
# Start / stop it now:            Start-ScheduledTask / Stop-ScheduledTask "AfterWord server"
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$name = "AfterWord server"

if ($Remove) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    "Removed the '$name' task."
    return
}

$script = Join-Path $PSScriptRoot "run_home.ps1"
# conhost --headless: no console window at all.
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "AfterWord on the home network (http://afterword.local)" -Force | Out-Null
"Registered '$name': it starts when you log in. Starting it now..."
Start-ScheduledTask -TaskName $name
