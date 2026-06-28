#!/usr/bin/env bash
# T014: download one small quantized GGUF LLM sized to fit VRAM_GB (hardware-profile.md).
# Default: Qwen2.5-7B-Instruct Q4_K_M (~4.7 GB) — fits the 12 GB profile with room to spare.
set -euo pipefail

DEST="${DEST:-$HOME/models/gguf}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf}"

mkdir -p "$DEST"
FILE="$DEST/$(basename "$MODEL_URL")"
echo ">> Downloading $(basename "$FILE") -> $DEST"
curl -fL -C - -o "$FILE" "$MODEL_URL"
echo ">> Saved: $FILE"
ls -lh "$FILE"
