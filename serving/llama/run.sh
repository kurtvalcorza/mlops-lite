#!/usr/bin/env bash
# Start the native llama-server supervisor on the WSL GPU host (Phase 3, hybrid GPU v1.2.0).
# The supervisor loads the model on demand, frees VRAM after idle, and proxies inference.
# Run from WSL:  bash serving/llama/run.sh
set -euo pipefail

export LLAMA_BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
export MODEL="${MODEL:-$HOME/models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf}"
export VRAM_GB="${VRAM_GB:-12}"
export IDLE_TIMEOUT="${IDLE_TIMEOUT:-120}"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "llama-server not found at $LLAMA_BIN — build it first." >&2; exit 1
fi
if [[ ! -f "$MODEL" ]]; then
  echo "model not found at $MODEL — run scripts/seed_models.sh first." >&2; exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo ">> Starting supervisor (model loads on first request, frees VRAM after ${IDLE_TIMEOUT}s idle)"
exec python3 "$DIR/supervisor.py"
