#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the alphamind-monitor Windows service via NSSM.

.DESCRIPTION
    Installs and configures the alphamind-monitor service with:
    - python -m alphamind.execution.continuous_monitor run as the entry point
    - AppExit Default Restart with a 60 s throttle
    - Automatic (Delayed Start) start type
    - Stdout / stderr redirected to %USERPROFILE%\AlphaMind\logs\
    - AppDirectory set to the project root

    The ObjectName (service account) configuration is left as a manual
    final step documented in RUNBOOK_end_to_end_verification.md to avoid
    exposing passwords in shell history.

    Service name parallels alphamind-collector and alphamind-scheduler.
    The three services share the log directory and .env layout; each
    owns its own log file (collector.log, pipeline.log, monitor.log).

.NOTES
    Prerequisites:
    - NSSM must be on the PATH (https://nssm.cc/)
    - Run from the project root after "uv sync"
    - Tested on Windows 10/11 with PowerShell 5.1+
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$ServiceName = 'alphamind-monitor'

# Resolve the project root as the directory containing this script's parent.
# Script lives in <repo>\scripts\; project root is one level up.
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

# Resolve the Python executable inside the uv-managed virtual environment.
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $PythonExe)) {
    Write-Error ("Python executable not found at '$PythonExe'. " +
                 "Run 'uv sync' from the project root first.")
    exit 1
}

# Resolve the log directory using the operator's profile.
$LogDir = Join-Path $env:USERPROFILE 'AlphaMind\logs'

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    Write-Host "Created log directory: $LogDir"
}

$StdoutLog = Join-Path $LogDir 'monitor.out.log'
$StderrLog = Join-Path $LogDir 'monitor.err.log'

# ---------------------------------------------------------------------------
# Verify NSSM is available
# ---------------------------------------------------------------------------

if (-not (Get-Command 'nssm' -ErrorAction SilentlyContinue)) {
    Write-Error ("'nssm' not found on PATH. " +
                 "Download from https://nssm.cc/ and place it in a directory on PATH.")
    exit 1
}

# ---------------------------------------------------------------------------
# Install the service
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Installing service: $ServiceName"
Write-Host "  Python:      $PythonExe"
Write-Host "  ProjectRoot: $ProjectRoot"
Write-Host "  Stdout log:  $StdoutLog"
Write-Host "  Stderr log:  $StderrLog"
Write-Host ""

# Install: nssm install <service> <exe> <args...>
# NSSM passes the arguments verbatim to the executable.
nssm install $ServiceName $PythonExe '-m' 'alphamind.execution.continuous_monitor' 'run'

# Set the working directory so relative config paths resolve correctly.
nssm set $ServiceName AppDirectory $ProjectRoot

# Restart on any exit condition.
nssm set $ServiceName AppExit Default Restart

# Throttle crash-restart loop: wait 60 000 ms (60 s) before restarting.
nssm set $ServiceName AppRestartDelay 60000

# Automatic (Delayed Start) — comes up after Windows finishes boot-time services.
nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START

# Redirect stdout and stderr to the log directory.
nssm set $ServiceName AppStdout $StdoutLog
nssm set $ServiceName AppStderr $StderrLog

# Append to log files rather than overwriting on each service restart.
nssm set $ServiceName AppStdoutCreationDisposition 4
nssm set $ServiceName AppStderrCreationDisposition 4

# ---------------------------------------------------------------------------
# Summary and next steps
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Service '$ServiceName' installed successfully."
Write-Host ""
Write-Host "NEXT STEP — configure the service account (required for correct"
Write-Host "%USERPROFILE% resolution and network credentials):"
Write-Host ""
Write-Host "  nssm set $ServiceName ObjectName <DOMAIN>\<USER> <PASSWORD>"
Write-Host ""
Write-Host "  Replace <DOMAIN>\<USER> with your Windows account"
Write-Host "  (e.g. .\YourUsername for a local account)."
Write-Host "  Type the password when prompted."
Write-Host ""
Write-Host "Then start the service:"
Write-Host ""
Write-Host "  nssm start $ServiceName"
Write-Host "  Get-Service $ServiceName"
Write-Host ""
Write-Host "See RUNBOOK_end_to_end_verification.md for full operator instructions."
