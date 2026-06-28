#!/usr/bin/env bash
# Prove a trained LoRA adapter is *servable* (T033, US4): load the base GGUF + the adapter GGUF
# in llama.cpp and generate a token. Converts the small base to GGUF once if needed.
#
#   bash serving/llama/verify_lora.sh <adapter.gguf> [base_hf_id]
set -euo pipefail

ADAPTER="${1:?usage: verify_lora.sh <adapter.gguf> [base_hf_id]}"
BASE_HF="${2:-Qwen/Qwen2.5-0.5B-Instruct}"
LLAMA_DIR="${LLAMA_DIR:-$HOME/llama.cpp}"
VENV="${VENV:-$HOME/mlops-train}"
GGUF_DIR="${GGUF_DIR:-$HOME/models/gguf}"
BASE_GGUF="$GGUF_DIR/$(echo "$BASE_HF" | tr '/' '_')-f16.gguf"

CLI="$LLAMA_DIR/build/bin/llama-cli"
[[ -x "$CLI" ]] || { echo "llama-cli not found at $CLI" >&2; exit 1; }

if [[ ! -f "$BASE_GGUF" ]]; then
  echo ">> Converting base $BASE_HF -> GGUF (one-time)"
  # The HF model is already cached from training; convert from the cache snapshot.
  SNAP=$("$VENV/bin/python" - "$BASE_HF" <<'PY'
import sys
from huggingface_hub import snapshot_download
print(snapshot_download(sys.argv[1]))
PY
)
  "$VENV/bin/python" "$LLAMA_DIR/convert_hf_to_gguf.py" "$SNAP" --outfile "$BASE_GGUF" --outtype f16
fi

echo ">> Serving base + LoRA adapter, generating a token"
"$CLI" -m "$BASE_GGUF" --lora "$ADAPTER" -ngl 999 -no-cnv -p "What is MLOps-Lite?" -n 24 2>/dev/null
echo
echo ">> OK: base + adapter loaded and generated — the registered version is servable."
