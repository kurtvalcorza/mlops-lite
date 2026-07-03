# Bring up the stack pointed at the native WSL serving supervisor, auto-resolving its (dynamic) IP.
# The gateway runs in Rancher's WSL distro and cannot reach Ubuntu's supervisor via
# host.docker.internal (cross-distro), so we inject Ubuntu's current eth0 IP at compose-up time.
#
# Usage (PowerShell, from repo root), AFTER starting the native daemons in WSL:
#   bash hostagent/run.sh            # GPU host agent — serves the LLM engine (018 T358)
#   bash training/run.sh             # training daemon (optional, for US4 /runs)
#   bash serving/bento/run.sh        # vision service (optional, for US1 /vision)
#   ./scripts/serve_up.ps1
param([string]$Distro = "Ubuntu", [int]$AgentPort = 8100, [int]$TrainerPort = 8091, [int]$BentoPort = 8092)

$ip = (wsl.exe -d $Distro hostname -I).Trim().Split(' ')[0]
if (-not $ip) { Write-Error "Could not resolve $Distro IP"; exit 1 }

$env:AGENT_URL = "http://${ip}:${AgentPort}"
# 018 T358: the LLM engine is served by the host agent's /engines/llm sub-path (was the standalone
# llama supervisor on :8090). Byte-compatible /health, /infer, /infer/stream (FR-177).
$env:SERVING_URL = "http://${ip}:${AgentPort}/engines/llm"
$env:TRAINER_URL = "http://${ip}:${TrainerPort}"
$env:BENTO_URL = "http://${ip}:${BentoPort}"
Write-Host "host agent (llm)   -> $env:SERVING_URL"
Write-Host "training daemon    -> $env:TRAINER_URL"
Write-Host "vision (bento)     -> $env:BENTO_URL"
docker compose up -d
docker compose ps
