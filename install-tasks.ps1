# =============================================================================
#  Quant-Bridge — Windows Task Scheduler installer.
#
#  Registers two scheduled tasks under \Quant-Bridge\ so the system runs on
#  schedule without manual intervention during the 60-day prove-it window.
#
#  Run as the CURRENT USER (not admin) so the tasks inherit your environment
#  and write to user-accessible paths. The tasks run only when you're logged in
#  — fine for a desktop trading workstation.
#
#  ML retraining is intentionally NOT scheduled during the test window. The
#  model is held fixed so observed performance reflects the LIVE edge, not a
#  moving target. Refit manually after the 60-day window completes.
#
#  Usage:
#      powershell -NoProfile -ExecutionPolicy Bypass -File install-tasks.ps1
#      powershell -NoProfile -ExecutionPolicy Bypass -File install-tasks.ps1 -Uninstall
# =============================================================================

[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogsDir  = Join-Path $RepoRoot 'logs'
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }

# Prefer the project venv interpreter; fall back to PATH python
$PythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $PythonExe)) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    Write-Host "[FATAL] No venv at .venv and no python in PATH." -ForegroundColor Red
    Write-Host "        Rebuild: py -3.12 -m venv .venv; .venv\Scripts\python -m pip install -e ." -ForegroundColor Red
    exit 1
}

$TaskFolder = '\Quant-Bridge\'

function Remove-QBTask {
    param([string]$Name)
    $full = "$TaskFolder$Name"
    try {
        Unregister-ScheduledTask -TaskPath $TaskFolder -TaskName $Name -Confirm:$false -ErrorAction Stop
        Write-Host "  Removed: $full"
    } catch {
        Write-Host "  Not present: $full"
    }
}

function Register-QBTask {
    param(
        [string]$Name,
        [string]$Description,
        [string]$PythonArgs,
        [string]$LogPrefix,
        [datetime]$StartTime,
        [string[]]$DaysOfWeek
    )
    $full = "$TaskFolder$Name"
    Remove-QBTask -Name $Name

    # Wrap python call in a cmd.exe redirect so stdout/stderr land in dated log
    $LogPath = Join-Path $LogsDir "$LogPrefix`_%date:~10,4%-%date:~4,2%-%date:~7,2%.log"
    $cmd     = "/c cd /d `"$RepoRoot`" && `"$PythonExe`" -u $PythonArgs > `"$LogPath`" 2>&1"

    $action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $StartTime
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::FromHours(4)) `
        -RestartCount 1 `
        -RestartInterval ([TimeSpan]::FromMinutes(15))
    # Run as the current user, only when logged in
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask `
        -TaskPath $TaskFolder `
        -TaskName $Name `
        -Description $Description `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal | Out-Null

    Write-Host "  Registered: $full" -ForegroundColor Green
    Write-Host "    Schedule: $($DaysOfWeek -join ',') at $($StartTime.ToString('h:mm tt'))"
    Write-Host "    Log:      logs\$LogPrefix`_<date>.log"
}

if ($Uninstall) {
    Write-Host ""
    Write-Host "Uninstalling Quant-Bridge scheduled tasks..." -ForegroundColor Yellow
    Remove-QBTask -Name 'QB_PreMarket'
    Remove-QBTask -Name 'QB_EOD'
    # Try to remove the folder too (only succeeds if empty)
    try {
        $sched = New-Object -ComObject Schedule.Service
        $sched.Connect()
        $root = $sched.GetFolder('\')
        $root.DeleteFolder('Quant-Bridge', 0)
        Write-Host "  Removed folder: \Quant-Bridge"
    } catch {
        Write-Host "  Folder \Quant-Bridge not empty or already gone."
    }
    Write-Host ""
    Write-Host "Done." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "Installing Quant-Bridge scheduled tasks..." -ForegroundColor Cyan
Write-Host "  Python:    $PythonExe"
Write-Host "  Repo:      $RepoRoot"
Write-Host "  Run as:    $env:USERNAME (interactive, no admin needed)"
Write-Host ""

# 8:00 AM local time, Mon-Fri  — pre-market preparation
Register-QBTask `
    -Name 'QB_PreMarket' `
    -Description 'Quant-Bridge pre-market: top-up prices, refresh HMM, emit readiness verdict.' `
    -PythonArgs 'run_premarket.py' `
    -LogPrefix 'premarket' `
    -StartTime ([datetime]::Today.AddHours(8)) `
    -DaysOfWeek 'Monday','Tuesday','Wednesday','Thursday','Friday'

# 5:45 PM local time, Mon-Fri — full EOD pipeline
Register-QBTask `
    -Name 'QB_EOD' `
    -Description 'Quant-Bridge EOD pipeline: refresh CTB/short/Form4/13F/8-K + intelligence + HMM refit + ML scoring.' `
    -PythonArgs '-m signal_scanner.institutional_intel.jobs.run_eod_pipeline' `
    -LogPrefix 'eod' `
    -StartTime ([datetime]::Today.AddHours(17).AddMinutes(45)) `
    -DaysOfWeek 'Monday','Tuesday','Wednesday','Thursday','Friday'

Write-Host ""
Write-Host "Done. Verify with:  Get-ScheduledTask -TaskPath '\Quant-Bridge\*'" -ForegroundColor Cyan
Write-Host ""
Write-Host "Notes:"
Write-Host "  - Tasks fire only when this user is logged in."
Write-Host "  - Leave the machine on for overnight tasks to run."
Write-Host "  - ML retraining is intentionally NOT scheduled during the 60-day"
Write-Host "    test window. The model is held fixed so observed performance"
Write-Host "    reflects the live edge, not a moving target."
Write-Host ""
