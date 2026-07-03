#!/usr/bin/env bash
# Start the native GPU host agent (018 US2). Stdlib-only, like the supervisors it replaces;
# pynvml (optional, research R1) accelerates GPU reads when installed in the venv.
# Run from WSL:  bash hostagent/run.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
# Auto-load local secrets if present (FR-017) — same pattern as training/run.sh.
[[ -f "$REPO/.env" ]] && { set -a; . "$REPO/.env"; set +a; }

export VRAM_GB="${VRAM_GB:-12}"
export MLOPS_STATE_DIR="${MLOPS_STATE_DIR:-$HOME/.mlops-lite}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"   # platformlib + hostagent importable

exec python3 "$DIR/main.py"
