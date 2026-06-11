#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the alphamind-monitor + alphamind-monitor-watchdog Windows
    services via NSSM (ALP-941).

.DESCRIPTION
    The continuous monitor runs as its own NSSM service, supervised by a
    DEDICATED OUT-OF-PROCESS watchdog that probes the monitor's file heartbeat
    (%USERPROFILE%\AlphaMind\logs\monitor.heartbeat) and restarts the monitor
    when it goes stale. The watchdog is a separate service — the monitor's
    in-process stall watchdog runs on the very event loop it guards, so a
    frozen loop starves it and the process wedges silently with NSSM still
    reporting "Running" (the ALP-841 / ALP-941 failure class this topology
    makes unrepresentable). Mirrors install_safety_core_service.ps1.

    Two services are installed:

      alphamind-monitor
        python -m alphamind.execution.continuous_monitor run
        The monitor proper: precision/data tasks (options stops, greeks,
        entry-window, fill stream + recovery sweep). Beats monitor.heartbeat
        and arms the faulthandler deadman (monitor_faulthandler.log).

      alphamind-monitor-watchdog
        python -m alphamind.execution.continuous_monitor watchdog
        Probes the heartbeat file and runs `nssm restart alphamind-monitor`
        when the beat is older than monitor_watchdog_tick_seconds x
        watchdog_cadence_multiplier (config/continuous_monitor.yaml).

    Both services use:
    - AppExit Default Restart with a 60 s throttle
    - Automatic (Delayed Start) start type
    - Stdout / stderr redirected to %USERPROFILE%\AlphaMind\logs\
    - AppDirectory set to the project root

    The ObjectName (service account) configuration is left as a manual final
    step documented in RUNBOOK_end_to_end_verification.md to avoid exposing
    passwords in shell history.

    Service names parallel alphamind-collector / alphamind-scheduler /
    alphamind-safety-core. The watchdog must run under the SAME account as
    the monitor so `nssm restart` has permission to restart it.

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

$MonitorServiceName  = 'alphamind-monitor'
$WatchdogServiceName = 'alphamind-monitor-watchdog'

# Topology invariant (ALP-945, the 2026-06-10 incident): no service may
# declare an SCM dependency (DependOnService) on alphamind-monitor. The
# watchdog's `nssm restart` is stop + start, and the SCM refuses the stop
# while a dependent service runs — a wedged monitor would then be
# unrestartable for as long as the dependent stays up.

# Resolve the project root as the directory containing this script's parent.
# Script lives in <repo>\scripts\; project root is one level up.
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)

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
# Helper — install one NSSM service off the continuous_monitor module
# ---------------------------------------------------------------------------

function Install-MonitorService {
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
        'alphamind.execution.continuous_monitor' $Subcommand

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

Install-MonitorService `
    -ServiceName $MonitorServiceName `
    -Subcommand 'run' `
    -StdoutLog (Join-Path $LogDir 'monitor.out.log') `
    -StderrLog (Join-Path $LogDir 'monitor.err.log')

Install-MonitorService `
    -ServiceName $WatchdogServiceName `
    -Subcommand 'watchdog' `
    -StdoutLog (Join-Path $LogDir 'monitor_watchdog.out.log') `
    -StderrLog (Join-Path $LogDir 'monitor_watchdog.err.log')

# ---------------------------------------------------------------------------
# Summary and next steps
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Services '$MonitorServiceName' and '$WatchdogServiceName' installed successfully."
Write-Host ""
Write-Host "NEXT STEP — configure the service account for BOTH services (required"
Write-Host "for correct %USERPROFILE% resolution AND so the watchdog has permission"
Write-Host "to 'nssm restart $MonitorServiceName'):"
Write-Host ""
Write-Host "  nssm set $MonitorServiceName ObjectName <DOMAIN>\<USER> <PASSWORD>"
Write-Host "  nssm set $WatchdogServiceName ObjectName <DOMAIN>\<USER> <PASSWORD>"
Write-Host ""
Write-Host "  Replace <DOMAIN>\<USER> with your Windows account"
Write-Host "  (e.g. .\YourUsername for a local account)."
Write-Host "  Type the password when prompted."
Write-Host ""
Write-Host "Then start the monitor FIRST, then the watchdog:"
Write-Host ""
Write-Host "  nssm start $MonitorServiceName"
Write-Host "  nssm start $WatchdogServiceName"
Write-Host "  Get-Service $MonitorServiceName, $WatchdogServiceName"
Write-Host ""
Write-Host "See docs/runbooks/: services.md (service table, § 7), update-loop.md (§ 1.4/1.6), bootstrap.md (§ 2.5-2.6)"
Write-Host "and RUNBOOK_end_to_end_verification.md for full operator instructions."
