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

# The daemon set supervise.py will select (its default already includes `agent` since T358). Used
# only to detect a STALE pre-fold-in supervisor still up from before the gateway URL flip. Mirror
# supervise.py's legacy mapping (a `serving` override now means `agent`) so the stale check below
# fires even for an unchanged `SUPERVISE_DAEMONS=serving,...` override (Codex round 8, 018).
DESIRED="${SUPERVISE_DAEMONS:-agent,training,vision,embed,tabular,ui}"
DESIRED="${DESIRED//serving/agent}"

# Is a named daemon present in the running supervisor's status?
supervising() {
  curl -s "$STATUS" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if sys.argv[1] in [x.get("name") for x in d.get("daemons", [])] else 1)
' "$1"
}

relaunch() { ( cd "$REPO" && nohup python3 supervisor/supervise.py > "$HOME/supervise.log" 2>&1 & disown ); }

if curl -s "$STATUS" >/dev/null 2>&1; then
  # 018 T358: the LLM engine now lives in the `agent` daemon and the gateway's SERVING_URL points at
  # it. A supervisor left running from BEFORE the fold-in manages `serving` but not `agent`, so the
  # gateway would forward /infer to a dead :8100 backend. Detect that stale set and restart with the
  # current one; otherwise stay idempotent.
  if [[ ",$DESIRED," == *",agent,"* ]] && ! supervising agent; then
    echo "[up] supervisor on :${PORT} predates the agent fold-in (no 'agent') — restarting current set"
    bash "$REPO/scripts/supervisor_down.sh" >/dev/null 2>&1 || true
    relaunch
  else
    echo "[up] supervisor already running on :${PORT} (idempotent)"
  fi
else
  echo "[up] launching supervisor ..."
  relaunch
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
