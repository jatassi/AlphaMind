#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the alphamind-safety-core + alphamind-safety-core-watchdog
    Windows services via NSSM (ALP-857 / ADR-0004 — W4b).

.DESCRIPTION
    The isolated safety core (breach detection + price-staleness, the lone
    safety item with NO broker floor) runs as its own NSSM service, supervised
    by a DEDICATED OUT-OF-PROCESS watchdog that probes the safety core's file
    heartbeat and restarts it on staleness. The watchdog is a separate service —
    a loop-resident watchdog cannot catch a freeze of its own loop (the ALP-841
    mechanism this isolation makes unrepresentable).

    Two services are installed:

      alphamind-safety-core
        python -m alphamind.execution.continuous_monitor.safety_core run
        Reads the broker snapshot + live price stream, evaluates the no-floor
        safety items, beats a heartbeat file, writes NOTHING to the DB.

      alphamind-safety-core-watchdog
        python -m alphamind.execution.continuous_monitor.safety_core watchdog
        Probes the heartbeat file and runs `nssm restart alphamind-safety-core`
        when it goes stale.

    Both services use:
    - AppExit Default Restart with a 60 s throttle
    - Automatic (Delayed Start) start type
    - Stdout / stderr redirected to %USERPROFILE%\AlphaMind\logs\
    - AppDirectory set to the project root

    The ObjectName (service account) configuration is left as a manual final
    step documented in RUNBOOK_end_to_end_verification.md to avoid exposing
    passwords in shell history.

    Service names parallel alphamind-collector / alphamind-scheduler /
    alphamind-monitor. The watchdog must run under the SAME account as the
    safety core so `nssm restart` has permission to restart it.

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

$CoreServiceName     = 'alphamind-safety-core'
$WatchdogServiceName = 'alphamind-safety-core-watchdog'

# Resolve the project root as the directory containing this script's parent.
# Script lives in <repo>\scripts\; project root is one level up.
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
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

# ---------------------------------------------------------------------------
# Verify NSSM is available
# ---------------------------------------------------------------------------

if (-not (Get-Command 'nssm' -ErrorAction SilentlyContinue)) {
    Write-Error ("'nssm' not found on PATH. " +
                 "Download from https://nssm.cc/ and place it in a directory on PATH.")
    exit 1
}

# ---------------------------------------------------------------------------
# Helper — install one NSSM service off the safety_core module
# ---------------------------------------------------------------------------

function Install-SafetyCoreService {
    param(
        [string]$ServiceName,
        [string]$Subcommand,
        [string]$StdoutLog,
        [string]$StderrLog
    )

    Write-Host ""
    Write-Host "Installing service: $ServiceName"
    Write-Host "  Python:      $PythonExe"
    Write-Host "  Subcommand:  $Subcommand"
    Write-Host "  ProjectRoot: $ProjectRoot"
    Write-Host "  Stdout log:  $StdoutLog"
    Write-Host "  Stderr log:  $StderrLog"
    Write-Host ""

    # Install: nssm install <service> <exe> <args...>
    nssm install $ServiceName $PythonExe '-m' `
        'alphamind.execution.continuous_monitor.safety_core' $Subcommand

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
}

# ---------------------------------------------------------------------------
# Install both services
# ---------------------------------------------------------------------------

Install-SafetyCoreService `
    -ServiceName $CoreServiceName `
    -Subcommand 'run' `
    -StdoutLog (Join-Path $LogDir 'safety_core.out.log') `
    -StderrLog (Join-Path $LogDir 'safety_core.err.log')

Install-SafetyCoreService `
    -ServiceName $WatchdogServiceName `
    -Subcommand 'watchdog' `
    -StdoutLog (Join-Path $LogDir 'safety_core_watchdog.out.log') `
    -StderrLog (Join-Path $LogDir 'safety_core_watchdog.err.log')

# ---------------------------------------------------------------------------
# Summary and next steps
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Services '$CoreServiceName' and '$WatchdogServiceName' installed successfully."
Write-Host ""
Write-Host "NEXT STEP — configure the service account for BOTH services (required"
Write-Host "for correct %USERPROFILE% resolution AND so the watchdog has permission"
Write-Host "to 'nssm restart $CoreServiceName'):"
Write-Host ""
Write-Host "  nssm set $CoreServiceName ObjectName <DOMAIN>\<USER> <PASSWORD>"
Write-Host "  nssm set $WatchdogServiceName ObjectName <DOMAIN>\<USER> <PASSWORD>"
Write-Host ""
Write-Host "  Replace <DOMAIN>\<USER> with your Windows account"
Write-Host "  (e.g. .\YourUsername for a local account)."
Write-Host "  Type the password when prompted."
Write-Host ""
Write-Host "Then start the safety core FIRST, then the watchdog:"
Write-Host ""
Write-Host "  nssm start $CoreServiceName"
Write-Host "  nssm start $WatchdogServiceName"
Write-Host "  Get-Service $CoreServiceName, $WatchdogServiceName"
Write-Host ""
Write-Host "See RUNBOOK_production.md (service table, sections 1.4/1.6, 2.5-2.6, 7, 9)"
Write-Host "and RUNBOOK_end_to_end_verification.md for full operator instructions."
