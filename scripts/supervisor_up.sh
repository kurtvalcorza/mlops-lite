#!/usr/bin/env bash
# WSL helper for up_all (002 US3, T053): ensure the native-daemon supervisor is running and all
# three daemons report healthy. Idempotent — if a supervisor is already up on :8099, just wait for
# health rather than launching a second one.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${SUPERVISE_STATUS_PORT:-8099}"
STATUS="http://localhost:${PORT}/status"
TIMEOUT="${SUPERVISE_UP_TIMEOUT:-180}"

all_healthy() {
  local s
  s=$(curl -s "$STATUS" 2>/dev/null) || return 1
  printf '%s' "$s" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
ds = d.get("daemons", [])
sys.exit(0 if ds and all(x["state"] == "healthy" for x in ds) else 1)
'
}

if curl -s "$STATUS" >/dev/null 2>&1; then
  echo "[up] supervisor already running on :${PORT} (idempotent)"
else
  echo "[up] launching supervisor ..."
  ( cd "$REPO" && nohup python3 supervisor/supervise.py > "$HOME/supervise.log" 2>&1 & disown )
fi

deadline=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if all_healthy; then
    echo "[up] all daemons healthy:"
    curl -s "$STATUS"; echo
    exit 0
  fi
  sleep 3
done

echo "[up] TIMEOUT after ${TIMEOUT}s waiting for daemons; last status:" >&2
curl -s "$STATUS" >&2; echo >&2
exit 1
