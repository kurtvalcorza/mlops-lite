<#
.SYNOPSIS
  Generate local secrets into .env (T046, FR-017): random MinIO/Postgres/Grafana credentials and
  a gateway API key. No secret is ever committed — .env is git-ignored; .env.example only documents.

.DESCRIPTION
  Writes a fresh .env (non-secret defaults + generated secrets). Refuses to overwrite an existing
  .env unless -Force, so re-runs don't silently rotate live credentials. Prints the API key once.

  020 (T401) modes for the Garage store, whose credential flow is REVERSED vs MinIO
  (contracts/store-migration.md §bootstrap — the store MINTS the S3 key pair, the operator
  records it; MinIO's pair was generated here and fed INTO the store):
    -EnsureGarageSecrets  append GARAGE_RPC_SECRET / GARAGE_ADMIN_TOKEN to an existing .env
                          if missing (needed BEFORE garage first boots; idempotent)
    -RecordGarage         run the idempotent garage-init one-shot and record the emitted
                          GARAGE_ACCESS_KEY_ID / GARAGE_SECRET_ACCESS_KEY into .env
                          (an already-recorded pair is kept and re-validated, never re-minted)

.EXAMPLE
  ./scripts/gen_secrets.ps1
  ./scripts/gen_secrets.ps1 -Force
  ./scripts/gen_secrets.ps1 -EnsureGarageSecrets
  ./scripts/gen_secrets.ps1 -RecordGarage
#>
param([switch]$Force, [switch]$EnsureGarageSecrets, [switch]$RecordGarage)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root ".env"

function New-Secret([int]$bytes = 24) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($b)
    # Base64 then strip non-alphanumerics -> shell/URL-safe credential.
    return ([Convert]::ToBase64String($b) -replace '[^A-Za-z0-9]', '')
}

function New-HexSecret([int]$bytes = 32) {
    # Garage rpc_secret must be a 32-byte hex string.
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($b)
    return (($b | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-EnvValue([string]$name) {
    if (-not (Test-Path $envPath)) { return $null }
    $line = Select-String -Path $envPath -Pattern "^$name=" | Select-Object -First 1
    if ($null -eq $line) { return $null }
    return ($line.Line -split '=', 2)[1]
}

if ($EnsureGarageSecrets) {
    if (-not (Test-Path $envPath)) { Write-Error "no .env at $envPath — run scripts/gen_secrets.ps1 first." }
    $added = $false
    if (-not (Get-EnvValue 'GARAGE_RPC_SECRET')) {
        Add-Content -Path $envPath -Value "`n# Garage (020 US1) — node RPC secret + admin-API token (generated; DO NOT COMMIT).`nGARAGE_RPC_SECRET=$(New-HexSecret 32)"
        $added = $true
    }
    if (-not (Get-EnvValue 'GARAGE_ADMIN_TOKEN')) {
        Add-Content -Path $envPath -Value "GARAGE_ADMIN_TOKEN=$(New-Secret 24)"
        $added = $true
    }
    if ($added) { Write-Host "Garage secrets ensured in $envPath." -ForegroundColor Green }
    else { Write-Host "Garage secrets already present — unchanged." }
    exit 0
}

if ($RecordGarage) {
    if (-not (Test-Path $envPath)) { Write-Error "no .env at $envPath — run scripts/gen_secrets.ps1 first." }
    Write-Host "Running the garage-init one-shot (idempotent) to obtain the store-minted key pair..."
    Push-Location $root
    try { $out = docker compose run --rm garage-init 2>&1 | Out-String } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($out -split "`n" | Select-Object -Last 5) -ForegroundColor Red
        Write-Error "garage-init failed — is the garage service up? (docker compose up -d garage)"
    }
    $key    = ($out -split "`r?`n" | Where-Object { $_ -match '^GARAGE_ACCESS_KEY_ID=' }    | Select-Object -Last 1) -replace '^GARAGE_ACCESS_KEY_ID=', ''
    $secret = ($out -split "`r?`n" | Where-Object { $_ -match '^GARAGE_SECRET_ACCESS_KEY=' } | Select-Object -Last 1) -replace '^GARAGE_SECRET_ACCESS_KEY=', ''
    if (-not $key -or -not $secret) { Write-Error "garage-init emitted no key pair — inspect: docker compose run --rm garage-init" }
    $existingKey = Get-EnvValue 'GARAGE_ACCESS_KEY_ID'
    $existingSecret = Get-EnvValue 'GARAGE_SECRET_ACCESS_KEY'
    if (($existingKey -and -not $existingSecret) -or (-not $existingKey -and $existingSecret)) {
        # Half a pair (hand-edit or an interrupted append): appending now would duplicate the
        # var that already exists. Fail loud; the operator reconciles .env first.
        Write-Error ".env holds a PARTIAL Garage pair (one of GARAGE_ACCESS_KEY_ID / GARAGE_SECRET_ACCESS_KEY without the other). Remove the stale line, then re-run."
    }
    if ($existingKey -and $existingSecret) {
        if ($existingKey -eq $key) {
            Write-Host "Garage key pair already recorded ($key) and matches the store — unchanged." -ForegroundColor Green
        } else {
            Write-Warning ".env has GARAGE_ACCESS_KEY_ID=$existingKey but the store's key is $key."
            Write-Warning "Keeping the recorded pair (never silently rotated). Reconcile manually if intended."
        }
        exit 0
    }
    Add-Content -Path $envPath -Value "`n# Garage S3 key pair — MINTED BY the store at bootstrap and recorded here`n# (scripts/gen_secrets -RecordGarage). DO NOT hand-edit or re-mint.`nGARAGE_ACCESS_KEY_ID=$key`nGARAGE_SECRET_ACCESS_KEY=$secret"
    Write-Host "Recorded Garage key pair ($key) into $envPath." -ForegroundColor Green
    exit 0
}

if ((Test-Path $envPath) -and -not $Force) {
    Write-Host ".env already exists at $envPath — not overwriting. Re-run with -Force to rotate." -ForegroundColor Yellow
    exit 0
}

$minioUser  = "mlops-" + (New-Secret 6)
$minioPass  = New-Secret 24
$pgPass     = New-Secret 24
$grafPass   = New-Secret 18
$apiKey     = "mll_" + (New-Secret 24)
$garageRpc  = New-HexSecret 32
$garageTok  = New-Secret 24

$content = @"
# MLOps-Lite — LOCAL SECRETS (generated by scripts/gen_secrets.ps1). DO NOT COMMIT.
# Non-secret settings keep their defaults from docker-compose.yml; override here if needed.

# Postgres (MLflow backend) — password is a generated secret.
POSTGRES_USER=mlops
POSTGRES_PASSWORD=$pgPass
POSTGRES_DB=mlflow
POSTGRES_PORT=55432

# MinIO (S3-compatible artifact + dataset storage) — generated credentials (no minioadmin).
MINIO_ROOT_USER=$minioUser
MINIO_ROOT_PASSWORD=$minioPass
MINIO_API_PORT=9000
MINIO_CONSOLE_PORT=9001

# Garage (020 US1) — replacement S3 store. RPC secret + admin token are generated here;
# the S3 key pair is store-minted AFTER bootstrap: run scripts/gen_secrets -RecordGarage
# once `docker compose up -d garage garage-init` has run.
GARAGE_S3_PORT=3900
GARAGE_RPC_SECRET=$garageRpc
GARAGE_ADMIN_TOKEN=$garageTok

# MLflow tracking + registry
MLFLOW_PORT=5500

# Gateway (FastAPI) + API-key auth (US1). Comma-separated to allow a small key set.
GATEWAY_PORT=8080
GATEWAY_API_KEYS=$apiKey

# Observability — Grafana admin password is a generated secret.
PROMETHEUS_PORT=9090
GRAFANA_PORT=3001
GRAFANA_USER=admin
GRAFANA_PASSWORD=$grafPass
"@

Set-Content -Path $envPath -Value $content -Encoding UTF8 -NoNewline
Write-Host "Wrote $envPath with generated secrets." -ForegroundColor Green
Write-Host ""
Write-Host "Gateway API key (send as the 'X-API-Key' header):" -ForegroundColor Cyan
Write-Host "  $apiKey"
Write-Host ""
Write-Host "For tests/clients, expose it in this shell:" -ForegroundColor Cyan
Write-Host "  `$env:GATEWAY_API_KEY = '$apiKey'"
