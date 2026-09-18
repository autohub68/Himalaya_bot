# One-time setup on Windows: runs the backend as a Scheduled Task that starts
# automatically at logon and stays running in the background.
# Run this from PowerShell in the project folder: .\scripts\install-service-windows.ps1

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $env:USERPROFILE ".venvs\him"
$TaskName = "HimHiringAssistant"

if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
    Write-Host "Creating virtual environment at $VenvDir"
    python -m venv $VenvDir
}

Write-Host "Installing dependencies"
& "$VenvDir\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$VenvDir\Scripts\python.exe" -m pip install --quiet -r "$ProjectDir\requirements.txt"

$Action = New-ScheduledTaskAction `
    -Execute "$VenvDir\Scripts\pythonw.exe" `
    -Argument "-m uvicorn app.main:app --host 127.0.0.1 --port 8765" `
    -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Backend installed as a scheduled task."
Write-Host "It will start automatically at every logon from now on."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName              # check status"
Write-Host "  Stop-ScheduledTask -TaskName $TaskName             # stop it"
Write-Host "  Start-ScheduledTask -TaskName $TaskName            # start it"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false   # uninstall"
