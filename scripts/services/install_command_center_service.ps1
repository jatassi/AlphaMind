#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the AlphaMindCommandCenter Windows service via NSSM.

.DESCRIPTION
    Installs and configures the AlphaMindCommandCenter service with:
    - python -m alphamind.command_center as the entry point
    - AppExit Default Restart with a 60 s throttle
    - Automatic (Delayed Start) start type
    - Stdout / stderr redirected to %USERPROFILE%\AlphaMind\logs\
    - AppDirectory set to the project root
    - No SCM service-ordering edges: the command center starts on its
      own, before or after its upstreams. Its SSE consumers reconnect
      with capped backoff until the scheduler / monitor come up
      (command_center/events/multiplexer.py), and the control proxy
      reaches upstreams per-request (ALP-945).

    The ObjectName (service account) configuration is left as a manual
    final step documented in docs/runbooks/command-center.md to avoid
    exposing passwords in shell history.

    Service name parallels alphamind-collector / alphamind-scheduler /
    alphamind-monitor; the four services share the log directory and
    .env layout; each owns its own log file (collector.log,
    pipeline.log, monitor.log, command_center.log).

.NOTES
    Prerequisites:
    - NSSM must be on the PATH (https://nssm.cc/)
    - Run from the project root after "uv sync"
    - The frontend bundle must exist under
      src/alphamind/command_center/frontend/dist/ (run "bun install
      --frozen-lockfile && bun run build" before this script — see
      docs/runbooks/command-center.md § Initial bring-up).
    - Tested on Windows 10/11 with PowerShell 5.1+
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$ServiceName = 'AlphaMindCommandCenter'

# Resolve the project root as the directory containing this script's parent.
# Script lives in <repo>\scripts\services\; project root is two levels up.
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
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

$StdoutLog = Join-Path $LogDir 'command_center.out.log'
$StderrLog = Join-Path $LogDir 'command_center.err.log'

# Resolve the expected frontend bundle path so this script can fail
# fast if the operator hasn't run `bun run build` yet — the command
# center boots fine without the bundle (StaticFiles mount skipped, API
# still works) but a service install with no SPA is almost certainly
# unintentional. Warning, not error, so the operator can opt to defer
# the build and still install the service.
$FrontendDist = Join-Path $ProjectRoot 'src\alphamind\command_center\frontend\dist'
if (-not (Test-Path $FrontendDist)) {
    Write-Warning ("Frontend bundle missing at '$FrontendDist'. " +
                   "The command center will serve only the API surface " +
                   "until you run 'bun install --frozen-lockfile && " +
                   "bun run build' in src/alphamind/command_center/frontend/.")
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
nssm install $ServiceName $PythonExe '-m' 'alphamind.command_center'

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

# Deliberately NO SCM service-ordering edge on the scheduler / monitor:
# the command center starts on its own — its SSE consumers reconnect with
# capped backoff until the upstreams come up (events/multiplexer.py), and
# the control proxy reaches them per-request. An SCM edge pointing at a
# watchdog-supervised service would also make the SCM refuse the watchdog's
# `nssm restart` (stop + start) while this service runs — the 2026-06-10
# 42-minute monitor wedge (ALP-945).

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
Write-Host "On first start, the command center prints a one-time setup token"
Write-Host "to '$StdoutLog'. Copy it and follow the passkey-registration"
Write-Host "section in docs/runbooks/command-center.md."
Write-Host ""
Write-Host "If you later edit bind.host for LAN access, Windows Firewall may"
Write-Host "prompt — see docs/runbooks/command-center.md § LAN access."
Write-Host ""
Write-Host "See docs/runbooks/command-center.md for the full operator runbook."
