#!/usr/bin/env bash
# Start the slim vision child natively in WSL (020 US2, T407 — replaces serving/bento/run.sh).
# Same launch contract as the BentoML child it replaces (contracts/children-api.md): the host
# agent spawns this with BENTO_HOST/BENTO_PORT injected (dynamic loopback port, process group);
# readiness = GET /readyz. Seed the model first:
#   ~/mlops-train/bin/python scripts/seed_vision_model.py
# Standalone:  bash serving/children/run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
# Auto-load local secrets if present (FR-017): credentials come from .env, not hardcoded defaults.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

VENV="${VENV:-$HOME/mlops-train}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:9000}"
# Bridge object-store creds -> AWS_* for boto3; fail fast if neither is set (FR-017).
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER:?set MINIO_ROOT_USER (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export VISION_MODEL="${VISION_MODEL:-vision-mobilenet}"
PORT="${BENTO_PORT:-8092}"
HOST="${BENTO_HOST:-0.0.0.0}"

if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "uvicorn not found in $VENV — pip install -r serving/children/requirements.txt into it first." >&2
  exit 1
fi

cd "$DIR"
echo ">> Starting slim vision child on ${HOST}:$PORT (model loads from the object store on first request)"
exec "$VENV/bin/uvicorn" vision_service:app --host "$HOST" --port "$PORT"
