<#
.SYNOPSIS
  One-command teardown (002 US3, T054 / FR-019): stop the native daemons (no GPU orphans) then the
  Compose infra.

.DESCRIPTION
  1. Stops the supervisor + daemons via the WSL helper, which SIGTERMs the supervisor (graceful
     child shutdown), sweeps any stragglers, and verifies the GPU has no leftover compute processes.
  2. `docker compose down` the infra.
#>
param([string]$Distro = "Ubuntu")

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

Write-Host "[1/2] stopping native daemons (supervisor) ..." -ForegroundColor Green
wsl.exe -d $Distro bash scripts/supervisor_down.sh

Write-Host "`n[2/2] docker compose down ..." -ForegroundColor Green
docker compose down

Write-Host "`nPlatform DOWN — daemons stopped (VRAM released) and infra removed." -ForegroundColor Green
