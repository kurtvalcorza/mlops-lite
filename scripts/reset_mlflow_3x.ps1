<#
.SYNOPSIS
  One-time MLflow 2.18 -> 3.x backend reset (007 FR-055). DESTRUCTIVE.

.DESCRIPTION
  MLflow 3.x will NOT start against a 2.18 Postgres schema, and 007 deliberately chose a fresh-volume
  reset over an in-place `mlflow db upgrade`. Run this ONCE when upgrading an existing 2.18 install
  (i.e. a host that already has the `mlops-lite_pgdata` volume). A FRESH install does NOT need it —
  `up_all` inits a clean 3.x schema on first run.

  Steps: compose down -> drop ONLY `mlops-lite_pgdata` (Garage datasets/artifacts + Grafana survive) ->
  `up_all` (fresh 3.x backend) -> re-seed the serving LLM + vision registry pointers.

  TRADE: the MLflow run/trace history is dropped (accepted — grilled). Datasets persist (content-
  addressed on Garage) and are NOT touched.

.PARAMETER Confirm
  REQUIRED. Without it the script refuses to run, because it drops the MLflow history volume.
#>
param([switch]$Confirm, [string]$Distro = "Ubuntu")

# Guard FIRST, with a clean message + exit 1 (Write-Host, not Write-Error — the latter would throw once
# ErrorActionPreference is Stop, turning a deliberate refusal into an ugly terminating error).
if (-not $Confirm) {
    Write-Host ("DESTRUCTIVE: this drops the MLflow run/trace history (the mlops-lite_pgdata volume). " +
        "Garage datasets/artifacts are preserved. Re-run with -Confirm to proceed:`n" +
        "    .\scripts\reset_mlflow_3x.ps1 -Confirm") -ForegroundColor Yellow
    exit 1
}

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

Write-Host "[1/3] stopping the stack and dropping the MLflow Postgres volume (mlops-lite_pgdata) ..." -ForegroundColor Green
# `down` (no -v) removes containers but KEEPS named volumes; then drop ONLY pgdata. Never `down -v`
# (that would also wipe garagedata = datasets + model artifacts).
docker compose down
docker volume rm mlops-lite_pgdata
if ($LASTEXITCODE -ne 0) { Write-Error "could not drop mlops-lite_pgdata (is the stack fully down?)"; exit 1 }

# up_all is intentionally strict (it aborts if a native daemon isn't healthy within its window). A
# transient serving-daemon health flap during the rapid down/up can trip that, but it does NOT affect
# the re-seed — register/promote + vision seed only touch MLflow/Garage/the gateway, not the GPU daemons.
# So tolerate an up_all hiccup and let the RE-SEED be the real success gate (it needs the infra up).
Write-Host "`n[2/3] bringing the stack up on a fresh 3.x backend ..." -ForegroundColor Green
try { & "$PSScriptRoot/up_all.ps1" -Distro $Distro }
catch {
    Write-Warning ("up_all reported an issue ($($_.Exception.Message)). This is often a transient daemon " +
        "health flap during bring-up. Proceeding to re-seed (it needs only MLflow/Garage/gateway); " +
        "re-check the GPU daemons afterwards with:  wsl -d $Distro bash scripts/supervisor_up.sh")
}

Write-Host "`n[3/3] re-seeding the serving LLM + vision registry pointers ..." -ForegroundColor Green
wsl.exe -d $Distro bash scripts/reseed_registry.sh
if ($LASTEXITCODE -ne 0) { Write-Error "registry re-seed failed — fix the stack, then run 'wsl bash scripts/reseed_registry.sh'"; exit 1 }

Write-Host "`nMLflow 3.x reset complete — fresh backend, registry re-seeded, datasets preserved on Garage." -ForegroundColor Green
