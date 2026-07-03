#!/usr/bin/env bash
# Start the native GPU host agent (018 US2). Serves every engine AND — since T362 — the jobs
# surface (fine-tune / HPO / batch / shadow-replay), so it runs under the TRAINING venv: the
# in-process HPO/batch flows import torch/optuna/mlflow, and `run_flow.py`/`run_shadow.py`
# subprocesses inherit this interpreter (sys.executable) so they have the training stack too.
# Falls back to system python3 (serving still works; jobs fail as failed-jobs) if the venv is absent.
# Run from WSL:  bash hostagent/run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
# Auto-load local secrets if present (FR-017) — same pattern as the retired training/run.sh.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

export VRAM_GB="${VRAM_GB:-12}"
export MLOPS_STATE_DIR="${MLOPS_STATE_DIR:-$HOME/.mlops-lite}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"   # platformlib + hostagent importable
# Training env the folded-in jobs need (was training/run.sh's block): MLflow/MinIO + flow knobs.
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:5500}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:9000}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER:?set MINIO_ROOT_USER (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export LLAMA_DIR="${LLAMA_DIR:-$HOME/llama.cpp}"
export BASE_MODEL="${BASE_MODEL:-Qwen/Qwen2.5-0.5B-Instruct}"

VENV="${VENV:-$HOME/mlops-train}"
if [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
else
  echo ">> training venv not found at $VENV — running the agent under system python3; serving works," \
       "but jobs (fine-tune/HPO/batch/shadow) will fail until the venv is built." >&2
  PY="python3"
fi

exec "$PY" "$DIR/main.py"
