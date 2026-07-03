"""Serving client + GPU/lease-state surface (T016/T017; 008 US1/US3).

The gateway proxies inference to a native serving **supervisor** running on the WSL GPU host
(hybrid GPU, constitution v1.2.0). The supervisor owns the llama-server lifecycle (on-demand load,
idle VRAM release, oversize rejection).

**Principle II authority moved in 008** (Claude review F5): the single race-free **GPU lease**
(serving/gpu_lease.py), held by the native daemons, is now what guarantees one GPU tenant at a time
— **not** this gateway. The `_gpu_lock` below is only a gateway-side concurrency limiter (at most one
in-flight supervisor call); it no longer *enforces* Principle II. `gpu_state()` reads the lease
holder via the supervisor `/health` for the UI status line (FR-068).
"""
import asyncio

import httpx

from . import settings

SERVING_URL = settings.SERVING_URL
SERVING_MODEL = settings.SERVING_MODEL

# Shared-lockfile tenant id -> the label the UI status line shows (008 FR-068). The supervisor reads
# the single GPU lease (serving/gpu_lease.py) and reports its current holder in /health, so this one
# read covers every tenant (LLM, vision, training) — they all share the same lockfile.
_HOLDER_LABEL = {"llm-serving": "llm", "vision": "vision", "training": "training", "asr": "asr"}

_gpu_lock = asyncio.Lock()  # gateway-side concurrency limiter only; Principle II is the lease's job


class ServingError(Exception):
    """Serving backend unreachable or errored."""


class ModelTooLargeError(ServingError):
    """Requested model exceeds the VRAM budget (FR-004)."""


class ServingBusyError(ServingError):
    """Another GPU tenant holds the single slot — the refuse-if-held path (Principle II / 008).
    The host agent (018 T358) returns 409 for this; the retired supervisor lumped it into 507. It
    is contention, NOT a backend fault, so the router classifies it as `rejected` (not `error`)
    and surfaces it as a 409 rather than a 502."""


async def health() -> bool:
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{SERVING_URL}/health")
            return r.status_code == 200 and r.json().get("ok", False)
        except httpx.HTTPError:
            return False


async def gpu_state() -> dict:
    """Lease/GPU state for the UI status line (008 FR-068): which tenant holds the single GPU lease,
    the serving model name, and whether the LLM is resident.

    Sourced from the supervisor's /health, which reads the shared lease (gpu_lease.current_holder)
    — so one read reflects whichever tenant (LLM, vision, training) holds the GPU. Key-free: the
    BFF contract is unchanged (no key reaches the browser). Returns holder=None when unreadable.
    """
    holder, resident, model = None, False, SERVING_MODEL
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{SERVING_URL}/health")
            if r.status_code == 200:
                h = r.json()
                resident = bool(h.get("resident"))
                holder = _HOLDER_LABEL.get(h.get("lease_holder"), h.get("lease_holder"))
                model = h.get("model") or SERVING_MODEL
        except httpx.HTTPError:
            pass
    return {"holder": holder, "resident": resident, "serving_model": model}


async def run_inference(prompt: str, max_tokens: int = 256, temperature: float = 0.7) -> dict:
    """Serialized call to the supervisor; returns {text, load_ms, infer_ms, model, usage}."""
    async with _gpu_lock:
        async with httpx.AsyncClient(timeout=300) as client:
            try:
                r = await client.post(
                    f"{SERVING_URL}/infer",
                    json={"prompt": prompt, "max_tokens": max_tokens, "temperature": temperature},
                )
            except httpx.HTTPError as e:
                raise ServingError(f"serving supervisor unreachable at {SERVING_URL}: {e}")
    if r.status_code == 507:
        raise ModelTooLargeError(r.json().get("error", "model exceeds VRAM budget"))
    if r.status_code == 409:
        # 018 T358: the agent returns 409 when another GPU tenant holds the slot. Keep it a distinct
        # "busy" (not a generic ServingError → 502) so /infer reports it rejected, keeping the
        # pre-018 refuse-if-held client semantics the retired supervisor expressed as a 507.
        try:
            detail = r.json().get("error") or "GPU busy — another tenant holds the slot"
        except Exception:
            detail = "GPU busy — another tenant holds the slot"
        raise ServingBusyError(detail)
    if r.status_code != 200:
        raise ServingError(f"serving error {r.status_code}: {r.text[:200]}")
    return r.json()
