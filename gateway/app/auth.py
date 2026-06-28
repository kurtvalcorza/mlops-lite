"""Gateway API-key authentication (T044, FR-016/FR-022; 005 US2 fail-closed, FR-042).

A single local operator closes open access with a shared key (or a small key set), configured
locally — env `GATEWAY_API_KEYS` (comma-separated) or a secret file `GATEWAY_API_KEYS_FILE`
(one key per line). No managed identity provider (Principle I/III). Keys are compared in
constant time over their SHA-256 hashes and are never logged or echoed (FR-022).

Auth posture (resolved once at import; KEY ROTATION REQUIRES A GATEWAY RESTART, FR-046 — the
key set is read at startup and cached, so adding/removing a key in .env or the keys file takes
effect only after the gateway process restarts):

  - **keyed**         — keys configured → the `X-API-Key` header is required on protected routes.
                        This is the provisioned path (`scripts/gen_secrets` → `up_all`), unchanged.
  - **closed**        — no keys and no override (the DEFAULT, 005 US2) → the gateway still boots so
                        `/healthz`, `/metrics`, and `/` stay up for probes, but protected lifecycle
                        routes return 401 with guidance. Fail-closed, never silently open.
  - **open-override** — no keys but `GATEWAY_ALLOW_OPEN` is truthy → runs OPEN and logs a prominent
                        warning. The documented dev escape hatch; never the default.

`/healthz`, `/metrics`, and `/` are always open for probes — only the lifecycle routers depend
on `require_api_key`.
"""
import hashlib
import hmac
import logging
import os
from pathlib import Path

from fastapi import Header, HTTPException, status

logger = logging.getLogger("gateway.auth")

API_KEY_HEADER = "X-API-Key"

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _load_keys() -> list[str]:
    """Collect configured keys from the env var and/or the optional secret file."""
    keys: list[str] = []
    env = os.getenv("GATEWAY_API_KEYS", "")
    keys.extend(k for k in (s.strip() for s in env.split(",")) if k)

    path = os.getenv("GATEWAY_API_KEYS_FILE")
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            k = line.strip()
            if k and not k.startswith("#"):
                keys.append(k)
    return keys


def _hash(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


_KEY_HASHES = [_hash(k) for k in _load_keys()]
_KEYED = bool(_KEY_HASHES)
_ALLOW_OPEN = _truthy(os.getenv("GATEWAY_ALLOW_OPEN"))

# Resolved posture: keyed > open-override > closed (the fail-closed default).
if _KEYED:
    _MODE = "keyed"
elif _ALLOW_OPEN:
    _MODE = "open-override"
else:
    _MODE = "closed"

if _MODE == "keyed":
    logger.info("API-key auth ENABLED (mode=keyed, %d key(s) configured)", len(_KEY_HASHES))
elif _MODE == "open-override":
    logger.warning(
        "API-key auth OPEN (mode=open-override) — no GATEWAY_API_KEYS but GATEWAY_ALLOW_OPEN is "
        "set; the gateway serves protected routes UNAUTHENTICATED. Dev escape hatch only — unset "
        "GATEWAY_ALLOW_OPEN and run scripts/gen_secrets for any non-throwaway use."
    )
else:  # closed
    logger.warning(
        "API-key auth FAIL-CLOSED (mode=closed) — no GATEWAY_API_KEYS configured; protected "
        "lifecycle routes will return 401. Set GATEWAY_API_KEYS (run scripts/gen_secrets) to "
        "enable access, or set GATEWAY_ALLOW_OPEN=1 to run open (dev only)."
    )


def auth_enabled() -> bool:
    """True when at least one API key is configured (the keyed, provisioned path)."""
    return _KEYED


def auth_mode() -> str:
    """Resolved auth posture: 'keyed', 'closed' (fail-closed default), or 'open-override'."""
    return _MODE


def _valid(presented: str) -> bool:
    """Constant-time membership check against every configured key (no early exit, FR-022)."""
    candidate = _hash(presented)
    ok = False
    for h in _KEY_HASHES:
        ok |= hmac.compare_digest(candidate, h)
    return ok


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
):
    """FastAPI dependency guarding protected lifecycle routes (fail-closed, FR-042).

    - keyed         → 401 unless a configured key is presented.
    - open-override → pass through (dev escape hatch, warned at startup).
    - closed        → 401 with guidance to configure a key or set the override.

    The response is non-leaky — it never echoes the presented key nor any internal detail.
    """
    if _MODE == "open-override":
        return
    if _MODE == "closed":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="gateway is fail-closed: set GATEWAY_API_KEYS (run scripts/gen_secrets) "
            "or GATEWAY_ALLOW_OPEN=1 for dev",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
    # keyed
    if not x_api_key or not _valid(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
