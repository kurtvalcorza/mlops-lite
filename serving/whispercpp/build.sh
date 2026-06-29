#!/usr/bin/env bash
# Build whisper.cpp from source with CUDA on the WSL GPU host (009 US3, T165 — FR-079/084).
# Mirrors the llama.cpp build pattern: clone + cmake with the CUDA backend, then fetch a base model.
# whisper.cpp is a native CUDA GPU service already permitted under the v1.2.0 hybrid-GPU amendment —
# no resident cost until used (the ASR supervisor loads it on demand + idle-releases VRAM).
#
# Gate zero (T154): nvidia-smi must succeed in this env before building. Run from WSL:
#   bash serving/whispercpp/build.sh
set -euo pipefail

SRC_DIR="${WHISPER_SRC:-$HOME/whisper.cpp}"
MODEL_DIR="${WHISPER_MODEL_DIR:-$HOME/models/whisper}"
MODEL_NAME="${WHISPER_MODEL_NAME:-base.en}"   # ggml-base.en.bin (~150 MB) — tiny VRAM, fast

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi not found — whisper.cpp CUDA build requires the GPU env (gate zero, T154)." >&2
  exit 1
fi

if [[ ! -d "$SRC_DIR" ]]; then
  echo ">> cloning whisper.cpp -> $SRC_DIR"
  git clone https://github.com/ggml-org/whisper.cpp "$SRC_DIR"
fi

echo ">> building whisper.cpp with CUDA (cmake -DGGML_CUDA=ON) ..."
cmake -S "$SRC_DIR" -B "$SRC_DIR/build" -DGGML_CUDA=ON -DWHISPER_BUILD_SERVER=ON
cmake --build "$SRC_DIR/build" -j --config Release --target whisper-server

SERVER_BIN="$SRC_DIR/build/bin/whisper-server"
if [[ ! -x "$SERVER_BIN" ]]; then
  echo "build did not produce $SERVER_BIN — check the cmake output above." >&2
  exit 1
fi
echo ">> built: $SERVER_BIN"

mkdir -p "$MODEL_DIR"
MODEL_FILE="$MODEL_DIR/ggml-${MODEL_NAME}.bin"
if [[ ! -f "$MODEL_FILE" ]]; then
  echo ">> downloading whisper model ggml-${MODEL_NAME}.bin -> $MODEL_DIR"
  # whisper.cpp ships a downloader; fall back to a direct HF pull if it isn't present.
  if [[ -x "$SRC_DIR/models/download-ggml-model.sh" ]]; then
    ( cd "$SRC_DIR" && bash ./models/download-ggml-model.sh "$MODEL_NAME" )
    cp -f "$SRC_DIR/models/ggml-${MODEL_NAME}.bin" "$MODEL_FILE" 2>/dev/null || true
  fi
  [[ -f "$MODEL_FILE" ]] || curl -fL -o "$MODEL_FILE" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL_NAME}.bin"
fi
echo ">> model ready: $MODEL_FILE"
echo "done. start the ASR supervisor:  bash serving/whispercpp/run.sh"
