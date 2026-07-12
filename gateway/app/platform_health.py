"""Aggregated daemon health, surfaced through the one gateway entry point (002 US2, T051 / FR-015).

The supervisor (supervisor/supervise.py) keeps the native daemons alive; this lets an operator see
all three daemons' reachability via the gateway without knowing their injected WSL IPs. Reuses the
same daemon URLs the gateway already proxies to. Open (no API key) — it's a probe, like /healthz.
"""

import os

import httpx

from . import settings

AGENT_URL = settings.AGENT_URL
SERVING_URL = settings.SERVING_URL
TRAINER_URL = settings.TRAINER_URL
BENTO_URL = settings.BENTO_URL
EMBED_URL = settings.EMBED_URL
TABULAR_URL = settings.TABULAR_URL
ASR_URL = settings.ASR_URL

# T363: every engine + the jobs surface is now the ONE host agent, so instead of fanning out 6
# serial probes (all pointed at the agent anyway) read the agent's single `/health` once and derive
# each daemon's reachability from its `engines: {id: state}` map (+ the agent being up for jobs).
# name -> (engine id in the agent's /health | None for the jobs surface, display url kept for
# continuity — the byte-compatible per-engine health sub-path).
_DAEMONS = {
    "serving":  ("llm",     f"{SERVING_URL}/health"),
    "training": (None,      f"{TRAINER_URL}/health"),
    "vision":   ("vision",  f"{BENTO_URL}/readyz"),
    "embed":    ("embed",   f"{EMBED_URL}/readyz"),
    "tabular":  ("tabular", f"{TABULAR_URL}/readyz"),
    "asr":      ("asr",     f"{ASR_URL}/health"),
}
# Optional daemons: reported, but their absence does NOT fail `all_healthy` (Codex review). ASR
# (whisper.cpp) needs a manual CUDA build and is opt-in, so a host without it still comes up.
_OPTIONAL = {"asr"}
# Engine states that count as reachable/servable — mirrors the pre-T363 per-engine /health returning
# 200 when available, 503 when unavailable/disabled/wedged. A cold/idle engine is healthy.
_SERVABLE_STATES = {"cold", "loading", "ready"}


# The agent's /health probes each RESIDENT engine's `ready()` serially (~2s each); with several
# engines mid-cold-start (CPU embed/tabular are off-lease, so >1 can load at once) the read can
# exceed a tight bound. A generous single timeout keeps a stampede from tripping _agent_health to
# None — which, unlike the old per-daemon probes, marks EVERY daemon down at once (@claude PR#37).
# Deeper fix (a non-blocking agent /health) is a follow-up.
_AGENT_HEALTH_TIMEOUT_S = float(os.getenv("PLATFORM_HEALTH_TIMEOUT_S", "8"))


async def _agent_health():
    """One read of the agent's root /health; None if unreachable / non-200 / unparseable. Broadly
    best-effort — a health aggregator must never itself 500 on a probe failure."""
    try:
        async with httpx.AsyncClient(headers=settings.agent_headers(), timeout=_AGENT_HEALTH_TIMEOUT_S) as client:
            r = await client.get(f"{AGENT_URL}/health")
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 — probe, never propagate
        return None


async def aggregate() -> dict:
    """Per-daemon reachability + an overall flag, derived from the agent's single `/health` (T363).

    `all_healthy` reflects the REQUIRED daemons only — an opt-in daemon (ASR) that isn't built
    is still reported under `daemons`, but does not hold back bring-up.
    """
    agent = await _agent_health()
    engines = (agent or {}).get("engines", {})
    daemons = {}
    for name, (eid, url) in _DAEMONS.items():
        if eid is None:  # the jobs surface — reachable iff the agent responds
            reachable = agent is not None
        else:
            reachable = agent is not None and engines.get(eid) in _SERVABLE_STATES
        daemons[name] = {"reachable": reachable, "url": url, "optional": name in _OPTIONAL}
    return {
        "all_healthy": all(d["reachable"] for n, d in daemons.items() if n not in _OPTIONAL),
        "daemons": daemons,
    }
