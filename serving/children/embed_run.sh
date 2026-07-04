#!/usr/bin/env bash
# Start the slim embeddings child natively in WSL (020 US2, T408 — replaces
# serving/bento/embed_run.sh). CPU-only, OFF the GPU lease — an embed call succeeds even while a
# GPU tenant holds the lease. Same launch contract as the BentoML child (BENTO_HOST/BENTO_PORT
# injected by the agent; readiness = GET /readyz). Seed the model first:
#   ~/mlops-train/bin/python scripts/seed_embedding_model.py
# Standalone:  bash serving/children/embed_run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
# Auto-load local secrets if present (FR-017): credentials come from .env, not hardcoded defaults.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

VENV="${VENV:-$HOME/mlops-train}"
# Off-lease guarantee at the PROCESS level: MLflow's sentence_transformers flavor constructs
# `SentenceTransformer(...)` with no device arg, which auto-selects CUDA on a GPU host and would
# allocate VRAM *before* the service's `.to("cpu")` runs — violating off-lease + risking OOM of
# the active GPU tenant. Hiding the GPU makes this child unable to touch VRAM at all.
export CUDA_VISIBLE_DEVICES=""
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:${MLFLOW_PORT:-5500}}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:9000}"
# Bridge object-store creds -> AWS_* for the MLflow artifact download; fail fast if neither is set.
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER:?set MINIO_ROOT_USER (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export EMBED_MODEL="${EMBED_MODEL:-embed-minilm}"
PORT="${BENTO_PORT:-${EMBED_PORT:-8093}}"
HOST="${BENTO_HOST:-0.0.0.0}"

if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "uvicorn not found in $VENV — pip install -r serving/children/requirements.txt into it first." >&2
  exit 1
fi

cd "$DIR"
echo ">> Starting slim embeddings child on ${HOST}:$PORT (CPU, off-lease; model loads on first request)"
exec "$VENV/bin/uvicorn" embed_service:app --host "$HOST" --port "$PORT"
