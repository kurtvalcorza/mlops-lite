#!/usr/bin/env bash
# Start the slim tabular child natively in WSL (020 US2, T408 — replaces
# serving/bento/tabular_run.sh). CPU-only, OFF the GPU lease — a predict call succeeds even
# while a GPU tenant holds the lease. Same launch contract as the BentoML child
# (BENTO_HOST/BENTO_PORT injected by the agent; readiness = GET /readyz). Seed the model first:
#   ~/mlops-train/bin/python scripts/seed_tabular_model.py
# Standalone:  bash serving/children/tabular_run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
# Auto-load local secrets if present (FR-017): credentials come from .env, not hardcoded defaults.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

VENV="${VENV:-$HOME/mlops-train}"
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:${MLFLOW_PORT:-5500}}"  # resolve @serving
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:3900}"
# Bridge object-store creds -> AWS_* for boto3; fail fast if neither is set (FR-017).
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${GARAGE_ACCESS_KEY_ID:?set GARAGE_ACCESS_KEY_ID (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${GARAGE_SECRET_ACCESS_KEY:?set GARAGE_SECRET_ACCESS_KEY (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export TABULAR_MODEL="${TABULAR_MODEL:-tabular-lgbm}"
PORT="${BENTO_PORT:-${TABULAR_PORT:-8094}}"
HOST="${BENTO_HOST:-0.0.0.0}"

if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "uvicorn not found in $VENV — pip install -r serving/children/requirements.txt into it first." >&2
  exit 1
fi

cd "$DIR"
echo ">> Starting slim tabular child on ${HOST}:$PORT (CPU, off-lease; model loads on first request)"
exec "$VENV/bin/uvicorn" tabular_service:app --host "$HOST" --port "$PORT"
