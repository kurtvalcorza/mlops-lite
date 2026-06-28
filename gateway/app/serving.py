"""Serving client + one-model-in-VRAM guard (T016/T017).

The gateway proxies inference to a native serving **supervisor** running on the WSL GPU host
(hybrid GPU, constitution v1.2.0). The supervisor owns the llama-server lifecycle (on-demand
load, idle VRAM release, oversize rejection). A gateway-side asyncio lock serializes GPU use so
at most one inference is in flight — the gateway is the platform authority for Principle II.
"""
import asyncio
import os

import httpx

SERVING_URL = os.getenv("SERVING_URL", "http://host.docker.internal:8090")
SERVING_MODEL = os.getenv("SERVING_MODEL", "qwen2.5-7b-instruct-q4_k_m")

# Shared-lockfile tenant id -> the label the UI status line shows (008 FR-068). The supervisor reads
# the single GPU lease (serving/gpu_lease.py) and reports its current holder in /health, so this one
# read covers every tenant (LLM, vision, training) — they all share the same lockfile.
_HOLDER_LABEL = {"llm-serving": "llm", "vision": "vision", "training": "training"}

_gpu_lock = asyncio.Lock()


class ServingError(Exception):
    """Serving backend unreachable or errored."""


class ModelTooLargeError(ServingError):
    """Requested model exceeds the VRAM budget (FR-004)."""


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
    if r.status_code != 200:
        raise ServingError(f"serving error {r.status_code}: {r.text[:200]}")
    return r.json()
