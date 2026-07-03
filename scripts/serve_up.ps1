# Bring up the stack pointed at the native WSL serving supervisor, auto-resolving its (dynamic) IP.
# The gateway runs in Rancher's WSL distro and cannot reach Ubuntu's supervisor via
# host.docker.internal (cross-distro), so we inject Ubuntu's current eth0 IP at compose-up time.
#
# Usage (PowerShell, from repo root), AFTER starting the native daemons in WSL:
#   bash hostagent/run.sh            # GPU host agent — serves llm/asr/vision engines (018 T358-T360)
#   bash training/run.sh             # training daemon (optional, for US4 /runs)
#   ./scripts/serve_up.ps1
param([string]$Distro = "Ubuntu", [int]$AgentPort = 8100, [int]$TrainerPort = 8091)

$ip = (wsl.exe -d $Distro hostname -I).Trim().Split(' ')[0]
if (-not $ip) { Write-Error "Could not resolve $Distro IP"; exit 1 }

$env:AGENT_URL = "http://${ip}:${AgentPort}"
# 018 T358-T360: the LLM/ASR/vision engines are served by the host agent's /engines/<id> sub-paths
# (was the standalone llama :8090 / whisper :8095 / bento :8092 daemons). Byte-compatible surfaces.
$env:SERVING_URL = "http://${ip}:${AgentPort}/engines/llm"
$env:ASR_URL     = "http://${ip}:${AgentPort}/engines/asr"
$env:BENTO_URL   = "http://${ip}:${AgentPort}/engines/vision"
$env:TRAINER_URL = "http://${ip}:${TrainerPort}"
Write-Host "host agent (llm/asr/vision) -> $env:AGENT_URL"
Write-Host "training daemon             -> $env:TRAINER_URL"
docker compose up -d
docker compose ps
