#!/usr/bin/env bash
# Start the BentoML tabular service natively in WSL (009 US4, T172). CPU-only, OFF the GPU lease — a
# predict call succeeds even while a GPU tenant holds the lease. Reaches MinIO via localhost. Seed the
# model first:  ~/mlops-train/bin/python scripts/seed_tabular_model.py
# Then:  bash serving/bento/tabular_run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
# Auto-load local secrets if present (FR-017): credentials come from .env, not hardcoded defaults.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

VENV="${VENV:-$HOME/mlops-train}"
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:${MLFLOW_PORT:-5500}}"  # resolve @serving
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:9000}"
# Bridge MinIO creds -> AWS_* for boto3; fail fast if neither is set (no minioadmin default).
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER:?set MINIO_ROOT_USER (.env / scripts/gen_secrets)}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD (.env / scripts/gen_secrets)}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export TABULAR_MODEL="${TABULAR_MODEL:-tabular-lgbm}"
# 018 T361: the host agent spawns this as a child, injecting BENTO_HOST/BENTO_PORT (shared with the
# other bento children). Fall back to the standalone TABULAR_PORT / 0.0.0.0 for a legacy launch.
TABULAR_PORT="${BENTO_PORT:-${TABULAR_PORT:-8094}}"
TABULAR_HOST="${BENTO_HOST:-0.0.0.0}"

if [[ ! -x "$VENV/bin/bentoml" ]]; then
  echo "bentoml not found in $VENV — pip install bentoml lightgbm joblib into it first." >&2
  exit 1
fi

cd "$DIR"
echo ">> Starting BentoML tabular service on ${TABULAR_HOST}:$TABULAR_PORT (CPU, off-lease; model loads on first request)"
exec "$VENV/bin/bentoml" serve tabular_service:TabularService --host "$TABULAR_HOST" --port "$TABULAR_PORT"
