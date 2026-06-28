#!/usr/bin/env bash
# Native WSL launcher for the operator console (003 US1, T064/T067). The US2 supervisor runs this as
# a managed daemon and polls http://localhost:3000/healthz. Binds 127.0.0.1 ONLY (FR-025) — never the
# LAN, no TLS. The gateway API key is read from the repo .env and handed to the BFF via the
# environment; it is NEVER baked into the client bundle (FR-024).
set -euo pipefail

UI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$UI_DIR/.." && pwd)"
PORT="${UI_PORT:-3000}"

# Pull the gateway key from the same local secret source as 002 (first key in the list).
if [ -f "$REPO/.env" ]; then
  set -a; . "$REPO/.env"; set +a
fi
export GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
export GATEWAY_API_KEY="${GATEWAY_API_KEY:-$(printf '%s' "${GATEWAY_API_KEYS:-}" | cut -d, -f1)}"
export HOSTNAME=127.0.0.1
export PORT
export UI_PORT="${PORT}"  # the BFF's origin/Host guard validates against this (004 FR-033)

cd "$UI_DIR"

# 004 US2 (T088, FR-037): the build is owned by scripts/bootstrap.sh, NOT lazily here — building
# under the supervisor can overrun the bring-up timeout and needs network. Fail fast (don't build) if
# the tier wasn't provisioned, so a misconfigured machine is obvious instead of crash-looping a build.
if [ ! -d node_modules ] || [ ! -d .next ]; then
  echo "[ui] FATAL: the operator console is not provisioned (missing node_modules or .next)." >&2
  echo "[ui] Run the bootstrap first:  bash scripts/bootstrap.sh   (it installs Node deps + builds ui/)." >&2
  exit 1
fi

echo "[ui] starting on http://127.0.0.1:${PORT} (key server-side, gateway ${GATEWAY_URL})"
# exec the Next server directly (not via `npm run start`) so the supervised PID IS the long-lived
# server — no npm/sh wrapper that can exit while next-server keeps holding the port.
exec ./node_modules/.bin/next start -H 127.0.0.1 -p "${PORT}"
