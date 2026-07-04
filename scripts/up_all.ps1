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
param([string]$Distro = "Ubuntu", [int]$AgentPort = 8100)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (-not (Test-Path "$repo/.env")) {
    Write-Error "No .env found — run ./scripts/gen_secrets.ps1 first (secrets are required, FR-017)."
    exit 1
}

# 1. Resolve the (dynamic) WSL IP and wire the gateway -> the single host agent.
$ip = (wsl.exe -d $Distro hostname -I).Trim().Split(' ')[0]
if (-not $ip) { Write-Error "Could not resolve $Distro IP"; exit 1 }
# 018 T364: ALL five inference engines (llm/asr/vision/embed/tabular) AND the jobs surface
# (fine-tune/HPO/batch/shadow-replay) are served by the ONE host agent, so a single AGENT_URL is all
# the gateway needs — it derives each `${AGENT_URL}/engines/<id>` base + the legacy byte-compatible
# paths itself (gateway/app/settings.py). The six per-engine *_URL vars retired with the lockfile.
$env:AGENT_URL = "http://${ip}:${AgentPort}"
Write-Host "engines + jobs @ agent=$env:AGENT_URL (single endpoint)" -ForegroundColor Cyan

# 018 FR-174: point Prometheus's DIRECT agent scrape (file_sd) at the same injected distro IP, so
# GPU/holder/engine/job metrics stay observable when the gateway is down (host.docker.internal can't
# reach the Ubuntu agent cross-distro). Prometheus hot-reloads the file — no restart needed.
$targetsDir = Join-Path $repo "infra/prometheus/targets"
New-Item -ItemType Directory -Force $targetsDir | Out-Null
Set-Content -Path (Join-Path $targetsDir "hostagent.json") -Encoding utf8 `
    -Value "[{""targets"": [""${ip}:${AgentPort}""]}]"

# 2. Bring up the Compose infra (gateway inherits the daemon URLs above).
Write-Host "`n[1/3] docker compose up ..." -ForegroundColor Green
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Error "compose up failed"; exit 1 }

# Fail-fast hint (007 FR-055): MLflow 3.x will NOT start against a stale 2.18 Postgres schema. If the
# server doesn't go healthy shortly — the symptom when UPGRADING an existing 2.18 install — point the
# operator at the one-time fresh-volume reset rather than letting them debug a dead service.
$mlflowPort = if ($env:MLFLOW_PORT) { $env:MLFLOW_PORT } else { "5500" }
$mlflowOk = $false
foreach ($i in 1..20) {
    try { Invoke-WebRequest "http://127.0.0.1:$mlflowPort/health" -TimeoutSec 4 -UseBasicParsing | Out-Null; $mlflowOk = $true; break }
    catch { Start-Sleep 3 }
}
if (-not $mlflowOk) {
    Write-Warning ("MLflow is not healthy at 127.0.0.1:$mlflowPort. If you are UPGRADING from MLflow 2.18, " +
        "the 3.x server cannot start against the old Postgres schema — run the one-time reset:`n" +
        "    .\scripts\reset_mlflow_3x.ps1 -Confirm`n" +
        "(drops MLflow run/trace history; MinIO datasets/artifacts survive). Else: docker compose logs mlflow")
}

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
