"""Aggregated daemon health, surfaced through the one gateway entry point (002 US2, T051 / FR-015).

The supervisor (supervisor/supervise.py) keeps the native daemons alive; this lets an operator see
all three daemons' reachability via the gateway without knowing their injected WSL IPs. Reuses the
same daemon URLs the gateway already proxies to. Open (no API key) — it's a probe, like /healthz.
"""
import os

import httpx

SERVING_URL = os.getenv("SERVING_URL", "http://host.docker.internal:8090")
TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")
BENTO_URL = os.getenv("BENTO_URL", "http://host.docker.internal:8092")
EMBED_URL = os.getenv("EMBED_URL", "http://host.docker.internal:8093")

# name -> (health URL, whether 200 means ready). Bento services expose /readyz; the others /health.
# 009: embeddings (CPU, off-lease) is a per-modality reachability target (FR-085); ASR/tabular join in
# their phases.
_TARGETS = {
    "serving": f"{SERVING_URL}/health",
    "training": f"{TRAINER_URL}/health",
    "vision": f"{BENTO_URL}/readyz",
    "embed": f"{EMBED_URL}/readyz",
}


async def aggregate() -> dict:
    """Best-effort probe of each daemon; returns per-daemon reachability + an overall flag."""
    daemons = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for name, url in _TARGETS.items():
            try:
                r = await client.get(url)
                daemons[name] = {"reachable": r.status_code == 200, "url": url}
            except httpx.HTTPError:
                daemons[name] = {"reachable": False, "url": url}
    return {
        "all_healthy": all(d["reachable"] for d in daemons.values()),
        "daemons": daemons,
    }
