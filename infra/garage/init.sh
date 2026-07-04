#!/bin/sh
# 020 T401 — garage-init one-shot entrypoint (compose service `garage-init`).
# The actual bootstrap lives in init.py: the pinned garage image is scratch-based (no shell),
# so instead of scripting the garage CLI inside it (research R2's original sketch), a pinned
# python:3.12-alpine one-shot drives the token-authed Admin API over the shared network
# namespace. Idempotent — safe to re-run any time (scripts/gen_secrets --record-garage does).
set -eu
exec python3 /init.py
