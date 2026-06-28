#!/usr/bin/env bash
# Gate Zero (T004): verify GPU access before any serving/training (GPU) work.
# Tries container GPU access first; if the engine cannot pass the GPU through, falls back to
# the native GPU host (hybrid model, constitution v1.2.0 — GPU services run natively in WSL).
set -uo pipefail

IMAGE="${CUDA_IMAGE:-nvidia/cuda:12.6.0-base-ubuntu22.04}"

echo ">> Gate Zero: trying container GPU access (${IMAGE}) ..."
if docker run --rm --gpus all "${IMAGE}" nvidia-smi >/dev/null 2>&1; then
  echo ">> PASSED (container): the engine can pass the GPU through."
  docker run --rm --gpus all "${IMAGE}" nvidia-smi -L
  exit 0
fi

echo ">> Container GPU not available — falling back to the native GPU host ..."
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  echo ">> PASSED (native): GPU usable on the host for native serving/training."
  nvidia-smi -L
  exit 0
fi

echo ">> Gate Zero FAILED — no GPU access via container or native host."
echo "   Ensure the NVIDIA driver + WSL CUDA libraries are present, or enable container GPU passthrough."
exit 1
