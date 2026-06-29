#!/usr/bin/env bash
# Start the native whisper.cpp ASR supervisor on the WSL GPU host (009 US3, T166 — hybrid GPU v1.2.0).
# The supervisor loads whisper-server on demand, holds the single GPU lease while resident, frees VRAM
# after idle, and proxies transcription. Build first:  bash serving/whispercpp/build.sh
# Run from WSL:  bash serving/whispercpp/run.sh
set -euo pipefail

export WHISPER_BIN="${WHISPER_BIN:-$HOME/whisper.cpp/build/bin/whisper-server}"
export WHISPER_MODEL="${WHISPER_MODEL:-$HOME/models/whisper/ggml-base.en.bin}"
export VRAM_GB="${VRAM_GB:-12}"
export ASR_IDLE_TIMEOUT="${ASR_IDLE_TIMEOUT:-120}"

if [[ ! -x "$WHISPER_BIN" ]]; then
  echo "whisper-server not found at $WHISPER_BIN — build it first (bash serving/whispercpp/build.sh)." >&2
  exit 1
fi
if [[ ! -f "$WHISPER_MODEL" ]]; then
  echo "whisper model not found at $WHISPER_MODEL — run bash serving/whispercpp/build.sh." >&2
  exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo ">> Starting ASR supervisor (whisper.cpp loads on first request, frees VRAM after ${ASR_IDLE_TIMEOUT}s idle)"
exec python3 "$DIR/supervisor.py"
