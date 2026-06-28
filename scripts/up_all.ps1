<#
.SYNOPSIS
  One-command bring-up (002 US3, T053 / FR-019): Compose infra + native daemons (under the
  supervisor) + automatic daemon-IP wiring, waiting until everything is ready.

.DESCRIPTION
  1. Requires .env (FR-017) — fails fast with guidance if missing.
  2. Resolves the dynamic WSL (Ubuntu) IP and wires the gateway -> daemon URLs (cross-distro).
  3. `docker compose up` the infra (the gateway picks up the URLs via env interpolation).
  4. Starts the native daemons under the supervisor (idempotent) and waits for health.
  5. Waits until the gateway itself resolves every daemon (`/platform/health`).
#>
param([string]$Distro = "Ubuntu", [int]$SupervisorPort = 8090, [int]$TrainerPort = 8091, [int]$BentoPort = 8092)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (-not (Test-Path "$repo/.env")) {
    Write-Error "No .env found — run ./scripts/gen_secrets.ps1 first (secrets are required, FR-017)."
    exit 1
}

# 1. Resolve the (dynamic) WSL IP and wire the gateway -> daemon URLs.
$ip = (wsl.exe -d $Distro hostname -I).Trim().Split(' ')[0]
if (-not $ip) { Write-Error "Could not resolve $Distro IP"; exit 1 }
$env:SERVING_URL = "http://${ip}:${SupervisorPort}"
$env:TRAINER_URL = "http://${ip}:${TrainerPort}"
$env:BENTO_URL   = "http://${ip}:${BentoPort}"
Write-Host "daemon URLs -> serving=$env:SERVING_URL training=$env:TRAINER_URL vision=$env:BENTO_URL" -ForegroundColor Cyan

# 2. Bring up the Compose infra (gateway inherits the daemon URLs above).
Write-Host "`n[1/3] docker compose up ..." -ForegroundColor Green
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Error "compose up failed"; exit 1 }

# 3. Start the native daemons under the supervisor and wait for their health.
Write-Host "`n[2/3] starting native daemons under the supervisor ..." -ForegroundColor Green
wsl.exe -d $Distro bash scripts/supervisor_up.sh
if ($LASTEXITCODE -ne 0) { Write-Error "supervisor did not bring all daemons healthy"; exit 1 }

# 4. Wait until the GATEWAY resolves every daemon via the injected IP.
Write-Host "`n[3/3] waiting for the gateway to resolve all daemons ..." -ForegroundColor Green
$gwPort = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { "8080" }
$gw = "http://localhost:$gwPort"
$deadline = (Get-Date).AddSeconds(90)
$resolved = $false
do {
    try {
        $h = Invoke-RestMethod "$gw/platform/health" -TimeoutSec 5
        if ($h.all_healthy) { $resolved = $true; break }
    } catch {}
    Start-Sleep 3
} while ((Get-Date) -lt $deadline)

if (-not $resolved) {
    Write-Warning "gateway could not resolve all daemons within the timeout; check 'docker compose logs gateway' and the supervisor (:8099/status)."
    exit 1
}

Write-Host "`nPlatform UP — infra healthy, all daemons supervised and reachable through the gateway." -ForegroundColor Green
docker compose ps
