# Bring up the stack pointed at the native WSL serving supervisor, auto-resolving its (dynamic) IP.
# The gateway runs in Rancher's WSL distro and cannot reach Ubuntu's supervisor via
# host.docker.internal (cross-distro), so we inject Ubuntu's current eth0 IP at compose-up time.
#
# Usage (PowerShell, from repo root), AFTER starting the native daemons in WSL:
#   bash hostagent/run.sh            # GPU host agent — serves all engines AND the jobs surface (T358-T362)
#   ./scripts/serve_up.ps1
param([string]$Distro = "Ubuntu", [int]$AgentPort = 8100)

$ip = (wsl.exe -d $Distro hostname -I).Trim().Split(' ')[0]
if (-not $ip) { Write-Error "Could not resolve $Distro IP"; exit 1 }

$env:AGENT_URL = "http://${ip}:${AgentPort}"
# 018 T358-T362: the llm/asr/vision engines are the agent's /engines/<id> sub-paths, and the jobs
# surface (train/study/batch/shadow-replay) is served at the agent ROOT — TRAINER_URL points there
# (byte-compatible legacy aliases + superset /health). Was the standalone llama/whisper/bento/trainer
# daemons. embed/tabular default via compose; up_all injects all six for a full bring-up.
$env:SERVING_URL = "http://${ip}:${AgentPort}/engines/llm"
$env:ASR_URL     = "http://${ip}:${AgentPort}/engines/asr"
$env:BENTO_URL   = "http://${ip}:${AgentPort}/engines/vision"
$env:TRAINER_URL = "http://${ip}:${AgentPort}"
Write-Host "host agent (engines + jobs) -> $env:AGENT_URL"
docker compose up -d
docker compose ps
