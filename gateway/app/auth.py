"""Gateway API-key authentication (T044, FR-016/FR-022).

A single local operator closes open access with a shared key (or a small key set), configured
locally — env `GATEWAY_API_KEYS` (comma-separated) or a secret file `GATEWAY_API_KEYS_FILE`
(one key per line). No managed identity provider (Principle I/III). Keys are compared in
constant time over their SHA-256 hashes and are never logged or echoed (FR-022).

If no keys are configured the gateway runs OPEN and logs a prominent warning. The hardened
bring-up (`scripts/gen_secrets`) always provisions a key, so the production path is
authenticated; the bare `make up` dev path stays usable. `/healthz`, `/metrics`, and `/` are
always open for probes — only the lifecycle routers depend on `require_api_key`.
"""
import hashlib
import hmac
import logging
import os
from pathlib import Path

from fastapi import Header, HTTPException, status

logger = logging.getLogger("gateway.auth")

API_KEY_HEADER = "X-API-Key"


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
_ENABLED = bool(_KEY_HASHES)

if _ENABLED:
    logger.info("API-key auth enabled (%d key(s) configured)", len(_KEY_HASHES))
else:
    logger.warning(
        "API-key auth DISABLED — no GATEWAY_API_KEYS configured; the gateway is OPEN. "
        "Set GATEWAY_API_KEYS (or run scripts/gen_secrets) to require a key."
    )


def auth_enabled() -> bool:
    """True when at least one API key is configured (auth is enforced)."""
    return _ENABLED


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
    """FastAPI dependency: 401 unless a configured API key is presented (when auth is enabled).

    The response is non-leaky — it never echoes the presented key nor any internal detail.
    """
    if not _ENABLED:
        return
    if not x_api_key or not _valid(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
