#!/usr/bin/env bash
# Idempotent native-env bootstrap (002 US4, T057 / FR-020). Provisions the WSL execution environment
# the hybrid-GPU daemons need — Python venv + pinned deps, model seeding — gated by a Gate-Zero GPU
# check (FR-020 / constitution Gate Zero). Re-running is a no-op that reports already-ready.
#
# The one-time CUDA llama.cpp build is VERIFIED here, not performed — it's a documented manual step
# (see README "Setup on a new machine"). Retarget by editing ONLY .specify/memory/hardware-profile.md.
#
#   bash scripts/bootstrap.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$HOME/mlops-train}"
PY="$VENV/bin/python"
CU_INDEX="https://download.pytorch.org/whl/cu128"
LLAMA_BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
GGUF="${MODEL:-$HOME/models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf}"
PROFILE="$REPO/.specify/memory/hardware-profile.md"

ok()   { echo "  [ok] $*"; }
step() { echo "  [..] $*"; }
fail() { echo "  [FAIL] $*" >&2; exit 1; }

# 0. Hardware profile — the single retarget point.
VRAM_GB="$(grep -E '\| .VRAM_GB. \|' "$PROFILE" 2>/dev/null | sed -E 's/.*\|[^0-9]*([0-9]+)[^0-9]*\|.*/\1/' | head -1)"
VRAM_GB="${VRAM_GB:-12}"
echo "[0/6] hardware profile: VRAM_GB=${VRAM_GB} (edit ${PROFILE#$REPO/} to retarget)"

# 1. Gate Zero — the native GPU must be visible before any GPU-bound provisioning.
echo "[1/6] Gate Zero (native GPU) ..."
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  ok "$(nvidia-smi -L | head -1)"
else
  fail "no native GPU visible (nvidia-smi). Ensure the NVIDIA driver + WSL CUDA libraries are present."
fi

# 2. Python venv (idempotent). Falls back through venv -> virtualenv -> pip-bootstrap (no sudo).
echo "[2/6] Python venv at ${VENV} ..."
if [ -x "$PY" ]; then
  ok "venv exists"
else
  step "creating venv"
  if python3 -m venv "$VENV" 2>/dev/null; then
    ok "created via python -m venv"
  elif command -v virtualenv >/dev/null 2>&1; then
    virtualenv "$VENV" && ok "created via virtualenv"
  else
    step "no venv/virtualenv module — bootstrapping pip + virtualenv into --user (no sudo)"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    python3 /tmp/get-pip.py --user --break-system-packages
    python3 -m pip install --user virtualenv
    python3 -m virtualenv "$VENV"
  fi
  [ -x "$PY" ] || fail "venv creation failed"
fi

# 3. Pinned dependencies. Only the torch-family cu128 wheels are expensive, so gate JUST those on import;
#    the requirements installs ALWAYS run (idempotent + fast when satisfied) so a version bump on an
#    existing venv actually lands — e.g. 007's mlflow-skinny 3.14 / prefect 3.7.6. A stale native client
#    against a newer MLflow server is unsupported (FR-055), so this must not be skipped.
echo "[3/6] pinned deps (torch cu128 + training + bento) ..."
# Every install is `|| fail`-guarded: the script runs `set -uo pipefail` WITHOUT -e, so an unguarded
# install failure would let bootstrap continue + report success, leaving a stale native client against
# the newer server — the exact skew this step exists to prevent. Abort loudly instead.
"$PY" -m pip install --upgrade pip -q || fail "pip self-upgrade failed"
if "$PY" -c 'import torch, torchvision' 2>/dev/null; then
  ok "torch-family already importable (cu128 download skipped)"
else
  step "pip installing torch cu128 (first run downloads torch — several minutes)"
  "$PY" -m pip install -q torch==2.11.0 torchvision==0.26.0 --index-url "$CU_INDEX" \
    || fail "torch cu128 install failed (check the driver vs the cu128 wheels)"
fi
"$PY" -m pip install -q -r "$REPO/training/requirements.txt" || fail "training requirements install failed"
"$PY" -m pip install -q -r "$REPO/serving/bento/requirements.txt" || fail "bento requirements install failed"
# fsspec hold (007): datasets 3.1.0 caps fsspec<=2024.9.0 but bentoml needs >=2025.7.0 — the re-resolve
# above can downgrade it and break bentoml. Pin to the validated 2026.6.0 LAST so it wins (overrides
# datasets' conservative cap; both work). See scripts/native_env.lock.
"$PY" -m pip install -q 'fsspec==2026.6.0' || fail "fsspec==2026.6.0 pin failed"
if "$PY" -c 'import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)'; then
  ok "torch.cuda available (capability $("$PY" -c 'import torch;print(torch.cuda.get_device_capability())'))"
else
  fail "torch installed but cannot see the GPU — check the cu128 wheels vs the driver."
fi

# 4. llama.cpp (CUDA) — VERIFY only; the build is a documented one-time manual step.
echo "[4/6] llama-server (CUDA build) ..."
if [ -x "$LLAMA_BIN" ]; then
  ok "found ${LLAMA_BIN}"
else
  fail "llama-server not found at ${LLAMA_BIN}. Build it once (README 'Setup on a new machine'):
    git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
    cmake -S ~/llama.cpp -B ~/llama.cpp/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120
    cmake --build ~/llama.cpp/build --config Release -j --target llama-server llama-cli"
fi

# 5. Seed models. The serving LLM is a local download (idempotent). The vision model goes to MinIO,
#    so it's seeded best-effort only when the infra is up and the object is missing.
echo "[5/6] seeding models ..."
if [ -f "$GGUF" ]; then
  ok "LLM gguf present ($(basename "$GGUF"))"
else
  step "downloading LLM gguf"
  bash "$REPO/scripts/seed_models.sh" || fail "LLM download failed"
fi

# Vision model -> MinIO (optional, idempotent guard).
if [ -f "$REPO/.env" ]; then set -a; . "$REPO/.env"; set +a; fi
MINIO_HEALTH="http://localhost:${MINIO_API_PORT:-9000}/minio/health/live"
if curl -fs "$MINIO_HEALTH" >/dev/null 2>&1 && [ -n "${MINIO_ROOT_USER:-}" ]; then
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$MINIO_ROOT_USER}"
  export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$MINIO_ROOT_PASSWORD}"
  export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://localhost:${MINIO_API_PORT:-9000}}"
  export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:${MLFLOW_PORT:-5500}}"
  if "$PY" - <<'PYEOF' 2>/dev/null
import os, sys, boto3
from botocore.client import Config
s3 = boto3.client("s3", endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"],
                  aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                  region_name="us-east-1", config=Config(signature_version="s3v4"))
try:
    s3.head_object(Bucket="models", Key="vision-mobilenet/v1/model.pt"); sys.exit(0)  # exists
except Exception:
    sys.exit(1)  # missing
PYEOF
  then
    ok "vision model already seeded in MinIO"
  else
    step "seeding vision model -> MinIO"
    "$PY" "$REPO/scripts/seed_vision_model.py" && ok "vision model seeded" || echo "  [warn] vision seed failed (non-fatal; serving smoke does not need it)"
  fi
else
  echo "  [skip] MinIO not up — seed the vision model later (after ./scripts/up_all.ps1):"
  echo "         ~/mlops-train/bin/python scripts/seed_vision_model.py"
fi

# 6. Operator console (003 UI) — Node/UI tier (004 US2, FR-037). Node gate (>= 20 LTS), then a
#    reproducible npm ci + next build so up_all starts the ui daemon WITHOUT building under the
#    supervisor (which can overrun the bring-up timeout and needs network). Idempotent.
echo "[6/6] operator console (Node/UI tier) ..."
NODE_MIN="${NODE_MIN:-20}"
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [ "${NODE_MAJOR:-0}" -ge "$NODE_MIN" ]; then
    ok "node $(node -v) / npm $(npm -v)"
  else
    fail "Node ${NODE_MIN} LTS+ required for the operator console (found $(node -v)). Install a current LTS (e.g. via nvm) and re-run."
  fi
else
  fail "node/npm not found — the operator console (ui/) needs Node ${NODE_MIN} LTS+. Install it (e.g. via nvm) and re-run."
fi
UI_DIR="$REPO/ui"
# Key on lockfile FRESHNESS, not directory existence — else a dependency bump (e.g. 007's React
# 19.0.0->19.2.7) never lands on a machine that already has node_modules/.next, leaving the console on
# the old tree + a stale build. npm writes node_modules/.package-lock.json as its installed-state marker;
# rebuild .next whenever package.json/lock changed since the last build.
LOCK="$UI_DIR/package-lock.json"; PKG="$UI_DIR/package.json"
if [ ! -d "$UI_DIR/node_modules" ] || [ "$LOCK" -nt "$UI_DIR/node_modules/.package-lock.json" ]; then
  step "npm ci (install/refresh — lockfile changed or first run)"
  ( cd "$UI_DIR" && npm ci --no-audit --no-fund ) || fail "npm ci failed in ui/"
else
  ok "ui dependencies up to date (lockfile unchanged since last install)"
fi
if [ ! -d "$UI_DIR/.next" ] || [ "$LOCK" -nt "$UI_DIR/.next" ] || [ "$PKG" -nt "$UI_DIR/.next" ]; then
  step "next build (refresh — deps/build changed or first run)"
  ( cd "$UI_DIR" && npm run build ) || fail "ui build failed"
else
  ok "ui already built (up to date)"
fi

echo
echo "Bootstrap complete — native env ready (venv, deps, GPU, llama-server, LLM model, operator UI)."
echo "Next:  ./scripts/up_all.ps1   then   python tests/test_serving.py  (console at http://127.0.0.1:3000)"
