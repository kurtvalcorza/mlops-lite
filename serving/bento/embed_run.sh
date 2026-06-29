#!/usr/bin/env bash
# Start the BentoML embeddings service natively in WSL (009 US2, T161). CPU-only, OFF the GPU lease —
# no GPU contention; an embed call succeeds even while a GPU tenant holds the lease. Reaches MLflow +
# MinIO via localhost. Seed the model first:
#   ~/mlops-train/bin/python scripts/seed_embedding_model.py
# Then:  bash serving/bento/embed_run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
# Auto-load local secrets if present (FR-017): credentials come from .env, not hardcoded defaults.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

VENV="${VENV:-$HOME/mlops-train}"
# Off-lease guarantee at the PROCESS level (Codex review): MLflow's sentence_transformers flavor
# constructs `SentenceTransformer(...)` with no device arg, which auto-selects CUDA on a GPU host and
# would allocate VRAM *before* the service's `.to("cpu")` runs — violating off-lease + risking OOM of
# the active GPU tenant. Hiding the GPU from this daemon makes the embeddings service unable to touch
# VRAM at all. (Embeddings *fine-tuning* is a separate GPU path; only serving is pinned to CPU here.)
export CUDA_VISIBLE_DEVICES=""
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:${MLFLOW_PORT:-5500}}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:9000}"
# Bridge MinIO creds -> AWS_* for the MLflow artifact download; fail fast if neither is set.
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER:?set MINIO_ROOT_USER (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export EMBED_MODEL="${EMBED_MODEL:-embed-minilm}"
EMBED_PORT="${EMBED_PORT:-8093}"

if [[ ! -x "$VENV/bin/bentoml" ]]; then
  echo "bentoml not found in $VENV — pip install bentoml sentence-transformers into it first." >&2
  exit 1
fi

cd "$DIR"
echo ">> Starting BentoML embeddings service on :$EMBED_PORT (CPU, off-lease; model loads on first request)"
exec "$VENV/bin/bentoml" serve embed_service:EmbeddingService --host 0.0.0.0 --port "$EMBED_PORT"
