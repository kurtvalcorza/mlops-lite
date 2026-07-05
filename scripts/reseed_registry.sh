#!/usr/bin/env bash
# Re-seed the MLflow registry after a fresh-backend reset (007 FR-055). The fresh-volume reset drops the
# MLflow Postgres store, so the registry pointers must be recreated for the platform to resolve again:
#   - serving LLM: register + promote `@serving` so `/infer`'s registry_version resolves,
#   - vision model: re-register the version (the model.pt object survives on Garage, but its registry
#     entry lived in pgdata) — seed_vision_model.py always creates a fresh version, so this re-registers.
# Datasets need NO re-seed (content-addressed on Garage). Re-runnable: each run registers a fresh version
# and promotes THAT version to @serving (older versions remain as registry history, none orphaned).
#
# Run in WSL after the stack is up:  bash scripts/reseed_registry.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -f .env ] || { echo "[FAIL] no .env — run scripts/gen_secrets first" >&2; exit 1; }
set -a; . ./.env; set +a

VENV="${VENV:-$HOME/mlops-train}"; PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "[FAIL] venv python not found at $PY — run scripts/bootstrap.sh" >&2; exit 1; }

GW="http://localhost:${GATEWAY_PORT:-8080}"
KEY="$(printf %s "${GATEWAY_API_KEYS:-}" | cut -d, -f1)"
[ -n "$KEY" ] || { echo "[FAIL] GATEWAY_API_KEYS not set in .env" >&2; exit 1; }

# 1. Vision model -> Garage + a fresh MLflow registry version.
echo "[1/2] re-seeding vision model (Garage object reused; registry version recreated) ..."
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$GARAGE_ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$GARAGE_SECRET_ACCESS_KEY}"
export MLFLOW_S3_ENDPOINT_URL="http://localhost:${GARAGE_S3_PORT:-3900}"
export MLFLOW_TRACKING_URI="http://localhost:${MLFLOW_PORT:-5500}"
"$PY" "$REPO/scripts/seed_vision_model.py" || echo "  [warn] vision seed failed (non-fatal)"

# 2. Serving LLM -> register + promote @serving via the gateway API. The registry entry is a routing
#    pointer (llama.cpp serves the GGUF locally; /infer never reads `source`), so an s3:// pointer is
#    used — MLflow 3.x rejects local file:// sources, and this mirrors the vision model's s3 source.
NAME="${SERVING_MODEL:-qwen2.5-7b-instruct-q4_k_m}"
SRC="s3://models/llm/${NAME}/Q4_K_M.gguf"
echo "[2/2] registering + promoting the serving LLM ($NAME) ..."
# Capture the register RESPONSE (body + status) so we promote the version we just created — not a
# hard-coded v1. On a re-run the gateway assigns v2/v3/..., and promoting THAT version (vs always v1)
# keeps the freshly-registered version as @serving instead of orphaning it behind a stale v1.
# 009 US1 (FR-074/086): tag with task=text-generation + serving_engine=llama.cpp so the gateway
# routes off registry metadata and the Infer tab renders the text-generation panel from it.
reg_resp="$(curl -s -w '\n%{http_code}' -X POST "$GW/models" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"name\":\"$NAME\",\"source\":\"$SRC\",\"tags\":{\"kind\":\"llm\",\"format\":\"gguf\",\"runtime\":\"llama.cpp\",\"task\":\"text-generation\",\"serving_engine\":\"llama.cpp\"}}")"
reg_code="$(printf '%s' "$reg_resp" | tail -n1)"
ver="$(printf '%s' "$reg_resp" | sed '$d' | grep -oE '"version"[[:space:]]*:[[:space:]]*"[0-9]+"' | grep -oE '[0-9]+' | head -1)"
[ "$reg_code" = "201" ] && echo "  registered $NAME v${ver:-?}" || echo "  [warn] register -> HTTP $reg_code (already present?)"
ver="${ver:-1}"  # fallback if the body couldn't be parsed (e.g. register skipped on an existing model)
prom_code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/models/$NAME/promote" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d "{\"version\":\"$ver\"}")"
[ "$prom_code" = "200" ] && echo "  promoted $NAME v$ver -> @serving" || { echo "  [FAIL] promote -> HTTP $prom_code" >&2; exit 1; }

echo "Re-seed complete — serving LLM @serving + vision registered; datasets intact on Garage."
