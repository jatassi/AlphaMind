#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Removes the alphamind-collector Windows service via NSSM.

.DESCRIPTION
    Stops the service if it is running and then removes it completely.
    Log files and the .env are left intact so a reinstall can resume
    without data loss.

.NOTES
    Prerequisites:
    - NSSM must be on the PATH (https://nssm.cc/)
    - Must be run as Administrator
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ServiceName = 'alphamind-collector'

# ---------------------------------------------------------------------------
# Verify NSSM is available
# ---------------------------------------------------------------------------

if (-not (Get-Command 'nssm' -ErrorAction SilentlyContinue)) {
    Write-Error ("'nssm' not found on PATH. " +
                 "Download from https://nssm.cc/ and place it in a directory on PATH.")
    exit 1
}

# ---------------------------------------------------------------------------
# Check that the service exists before trying to remove it
# ---------------------------------------------------------------------------

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Warning "Service '$ServiceName' is not registered — nothing to remove."
    exit 0
}

# ---------------------------------------------------------------------------
# Stop the service if it is running
# ---------------------------------------------------------------------------

if ($service.Status -ne 'Stopped') {
    Write-Host "Stopping service '$ServiceName'..."
    nssm stop $ServiceName confirm
    Write-Host "Service stopped."
}

# ---------------------------------------------------------------------------
# Remove the service
# ---------------------------------------------------------------------------

Write-Host "Removing service '$ServiceName'..."
nssm remove $ServiceName confirm
Write-Host "Service '$ServiceName' removed."

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "The service has been unregistered. The following artifacts are"
Write-Host "left intact and can be removed manually if no longer needed:"
Write-Host ""

$LogDir = Join-Path $env:USERPROFILE 'AlphaMind\logs'
Write-Host "  Log files:  $LogDir"
Write-Host "              (collector.log, collector.out.log, collector.err.log)"
Write-Host ""
Write-Host "  Config:     config\data_sources.yaml"
Write-Host "              config\collector_schedule.yaml"
Write-Host "              config\assets.yaml"
Write-Host "              .env"
Write-Host ""
Write-Host "To reinstall the service, run scripts\services\install_collector_service.ps1."
Write-Host "To reinstall from scratch, delete the log files and re-run bootstrap"
Write-Host "  python -m alphamind.collector bootstrap"
Write-Host "before reinstalling."
