#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Removes the alphamind-scheduler Windows service via NSSM.

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

$ServiceName = 'alphamind-scheduler'

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
Write-Host "              (pipeline.log, pipeline.out.log, pipeline.err.log)"
Write-Host ""
$ArchiveDir = Join-Path $env:USERPROFILE 'AlphaMind\archive'
Write-Host "  Archive:    $ArchiveDir"
Write-Host "              (invocations/ + process_lifetimes/ tree)"
Write-Host ""
Write-Host "  Config:     config\scheduler.yaml"
Write-Host "              config\run_types\*.yaml"
Write-Host "              config\breach_behavior.yaml"
Write-Host "              .env"
Write-Host ""
Write-Host "To reinstall the service, run scripts\install_pipeline_scheduler_service.ps1."
