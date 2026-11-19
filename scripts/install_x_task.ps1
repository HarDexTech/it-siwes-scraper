# Registers a daily Windows scheduled task that runs scripts/run_x.py.
# The script itself enforces the 48-hour rule, so most days it just logs "X skipped".
# Run once from the project folder:  powershell -ExecutionPolicy Bypass -File scripts\install_x_task.ps1
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$action = New-ScheduledTaskAction -Execute $python -Argument "scripts\run_x.py" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 10:00am
# StartWhenAvailable: if the PC was off at 10:00, run when it's next on.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "SIWES X collector" -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Registered 'SIWES X collector' (daily 10:00; X itself runs at most every 48h). Logs: data\logs\x.log"
