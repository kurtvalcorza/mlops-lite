"""Shared adapter helpers (018 US2) — kept deliberately tiny; per-engine specifics stay in each
adapter. This is where the llama/whisper copy that the 2026-07 architecture review flagged (§4.2)
converges: both GPU-lease serving supervisors returned a byte-identical /health and both picked a
dynamic child port the same way. One definition here, one behaviour for every lease tenant.
"""
import socket


def free_port() -> int:
    """An ephemeral loopback port for an engine's child process. The small bind→close→respawn TOCTOU
    window (same host, ephemeral range) is acceptable and removes the fixed-port EADDRINUSE class of
    failure the old supervisors had against an orphaned child on a hardcoded port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gpu_lease_health(lease, *, ok, reason, resident, model, vram_budget_gb, est_vram) -> dict:
    """The byte-compatible GPU-lease-tenant /health payload the retired llama + whisper supervisors
    both returned (gateway `serving.gpu_state` / the transcribe health probe read
    `ok`/`resident`/`model`/`lease_holder`). During the strangler migration `lease` (the legacy
    lockfile module) supplies the GLOBAL holder + free VRAM — the vision/other legacy daemons still
    share that lockfile, so the agent's in-process admission holder is not the whole truth; `lease`
    is None once the lockfile retires (T364) and the source flips to admission.

    `ok` is the caller's `available()` result (Codex round 7, 018): an engine whose binary/model is
    missing must report NOT ok so the gateway readiness aggregator + swap target-probe don't treat an
    unavailable engine as usable. A cold/idle-but-available engine stays `ok`."""
    holder = lease.current_holder() if lease else None
    free = lease.free_vram_gb() if lease else None
    payload = {
        "ok": ok,
        "resident": resident,
        "model": model,
        "vram_budget_gb": vram_budget_gb,
        "est_vram_gb": round(est_vram, 1) if est_vram is not None else None,
        "fits": (est_vram <= vram_budget_gb * 0.95) if est_vram is not None else None,
        "vram_free_gb": round(free, 1) if free is not None else None,
        "lease_holder": holder.get("tenant") if holder else None,
    }
    if not ok:
        payload["unavailable"] = reason
    return payload
