"""Aggregated daemon health, surfaced through the one gateway entry point (002 US2, T051 / FR-015).

The supervisor (supervisor/supervise.py) keeps the native daemons alive; this lets an operator see
all three daemons' reachability via the gateway without knowing their injected WSL IPs. Reuses the
same daemon URLs the gateway already proxies to. Open (no API key) — it's a probe, like /healthz.
"""

import httpx

from . import settings

SERVING_URL = settings.SERVING_URL
TRAINER_URL = settings.TRAINER_URL
BENTO_URL = settings.BENTO_URL
EMBED_URL = settings.EMBED_URL
TABULAR_URL = settings.TABULAR_URL
ASR_URL = settings.ASR_URL

# name -> health URL. Bento services expose /readyz; the supervised GPU daemons expose /health.
# 009: each new modality is a per-modality reachability target (FR-085) — embeddings + tabular (CPU,
# off-lease, Bento /readyz) and ASR (whisper.cpp GPU-lease supervisor, /health).
_TARGETS = {
    "serving": f"{SERVING_URL}/health",
    "training": f"{TRAINER_URL}/health",
    "vision": f"{BENTO_URL}/readyz",
    "embed": f"{EMBED_URL}/readyz",
    "tabular": f"{TABULAR_URL}/readyz",
    "asr": f"{ASR_URL}/health",
}
# Optional daemons: probed + reported, but their absence does NOT fail `all_healthy` (Codex review).
# ASR (whisper.cpp) needs a manual CUDA build and is opt-in in the supervisor's default set, so a host
# that hasn't built it must still bring the platform up cleanly (up_all gates on all_healthy).
_OPTIONAL = {"asr"}


async def aggregate() -> dict:
    """Best-effort probe of each daemon; returns per-daemon reachability + an overall flag.

    `all_healthy` reflects the REQUIRED daemons only — an opt-in daemon (ASR) that isn't built/running
    is still reported under `daemons`, but does not hold back bring-up.
    """
    daemons = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for name, url in _TARGETS.items():
            try:
                r = await client.get(url)
                daemons[name] = {"reachable": r.status_code == 200, "url": url,
                                 "optional": name in _OPTIONAL}
            except httpx.HTTPError:
                daemons[name] = {"reachable": False, "url": url, "optional": name in _OPTIONAL}
    return {
        "all_healthy": all(d["reachable"] for n, d in daemons.items() if n not in _OPTIONAL),
        "daemons": daemons,
    }
