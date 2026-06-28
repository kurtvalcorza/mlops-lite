#!/usr/bin/env bash
# Start the native training daemon on the WSL GPU host (Phase 6, hybrid GPU v1.2.0).
# Reaches MLflow/MinIO via localhost (WSL localhost-forwarding verified). Run from WSL:
#   bash training/run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
# Auto-load local secrets if present (FR-017): credentials come from .env, not hardcoded defaults.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

VENV="${VENV:-$HOME/mlops-train}"
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:5500}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:9000}"
# Bridge MinIO creds -> AWS_* for boto3; fail fast if neither is set (no minioadmin default).
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER:?set MINIO_ROOT_USER (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export LLAMA_DIR="${LLAMA_DIR:-$HOME/llama.cpp}"
export BASE_MODEL="${BASE_MODEL:-Qwen/Qwen2.5-0.5B-Instruct}"
export VRAM_GB="${VRAM_GB:-12}"
export SUPERVISOR_URL="${SUPERVISOR_URL:-http://localhost:8090}"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "training venv not found at $VENV — create it and install training/requirements.txt + torch (cu128)." >&2
  exit 1
fi

echo ">> Starting trainer (base=$BASE_MODEL, MLflow=$MLFLOW_TRACKING_URI)"
exec "$VENV/bin/python" "$DIR/trainer.py"
