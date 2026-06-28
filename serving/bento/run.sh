#!/usr/bin/env bash
# Start the BentoML vision service natively in WSL (Phase 8 / T022). CPU-only — no GPU contention.
# Reaches MinIO via localhost (WSL localhost-forwarding). Seed the model first:
#   ~/mlops-train/bin/python scripts/seed_vision_model.py
# Then:  bash serving/bento/run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
# Auto-load local secrets if present (FR-017): credentials come from .env, not hardcoded defaults.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

VENV="${VENV:-$HOME/mlops-train}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:9000}"
# Bridge MinIO creds -> AWS_* for boto3; fail fast if neither is set (no minioadmin default).
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER:?set MINIO_ROOT_USER (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export VISION_MODEL="${VISION_MODEL:-vision-mobilenet}"
BENTO_PORT="${BENTO_PORT:-8092}"

if [[ ! -x "$VENV/bin/bentoml" ]]; then
  echo "bentoml not found in $VENV — pip install bentoml torchvision pillow into it first." >&2
  exit 1
fi

cd "$DIR"
echo ">> Starting BentoML vision service on :$BENTO_PORT (model loads from MinIO on first request)"
exec "$VENV/bin/bentoml" serve service:VisionClassifier --host 0.0.0.0 --port "$BENTO_PORT"
