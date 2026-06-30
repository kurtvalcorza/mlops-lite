"""Swap-on-demand orchestration (017) — operator-confirmed preemptive GPU lease for *serving*.

008 shipped a cooperative **refuse-if-held** lease: a serving request is refused (409) while another
tenant holds the GPU. 017's A2 fast-follow lets an operator, with an explicit **`preempt=true`**, evict a
resident *serving* model so a different serving modality can load — a single sequential swap, **one model
in VRAM at any instant** (Principle II). A running **training/HPO/batch** job is **never** preempted.

This module is the gateway's swap broker (D2): it never holds the lease itself — it identifies the holder,
sends the holder's supervisor an `unload-now` control call, waits for the lease to free, and returns so the
caller's normal forward (which acquires the now-free lease and loads the target) proceeds. The default
(no `preempt`) path never calls in here, so 008 behavior is byte-for-byte unchanged (FR-161/SC-101).

Seams (`state_fn`, `http_post`, `sleep`) are injectable so the orchestration is unit-testable with the
lease + daemon HTTP mocked (no GPU, no live daemons).
"""
import asyncio
import os

# Per-supervisor drain bound (017 T324): how long a holder waits for in-flight requests to finish before a
# hard unload. Forwarded to the holder's `unload-now`; the supervisor enforces it (the gateway just states
# the intent). Kept small — a swap should be snappy.
SWAP_DRAIN_TIMEOUT_S = float(os.getenv("SWAP_DRAIN_TIMEOUT_S", "10"))

# How long the gateway waits for the lease to actually free after `unload-now` before giving up (the
# unload + VRAM teardown is bounded by the supervisor's own drain+kill, so this just backstops a wedge).
SWAP_FREE_WAIT_S = float(os.getenv("SWAP_FREE_WAIT_S", "30"))

# Holder LABEL (as reported by serving.gpu_state, mapped from the lease tenant) → the supervisor base URL
# that owns it and exposes `unload-now`. Only **serving** tenants are here; "training" is intentionally
# absent — it is never preemptable (FR-155) and the orchestrator refuses before any unload-now.
SERVING_HOLDER_URLS = {
    "llm": os.getenv("SERVING_URL", "http://host.docker.internal:8090"),
    "vision": os.getenv("BENTO_URL", "http://host.docker.internal:8092"),
    "asr": os.getenv("ASR_URL", "http://host.docker.internal:8095"),
}
# Tenants that hold the GPU for long-running work and must never be evicted by a swap (FR-155). "training"
# is the shared lease identity for fine-tune / HPO / batch runs (see training/trainer.py, hpo.py).
NON_PREEMPTABLE = {"training"}


class PreemptRefused(Exception):
    """A `preempt=true` swap was refused — the holder is a training/HPO/batch tenant (never preempted,
    FR-155) or could not be evicted. The router maps this to a clear 409."""


class SwapError(Exception):
    """The swap could not be completed (holder unreachable, lease never freed). Maps to 409/503."""


async def preempt_if_needed(target_label: str, *, state_fn=None, http_post=None, sleep=None,
                            drain_timeout_s: float = SWAP_DRAIN_TIMEOUT_S,
                            free_wait_s: float = SWAP_FREE_WAIT_S) -> dict:
    """Make room on the GPU for `target_label` when an operator opted into a swap (`preempt=true`).

    Returns a small dict describing what happened (`{"swapped": bool, "evicted": <label|None>, ...}`); the
    caller then runs its **normal forward**, which acquires the now-free lease and loads the target. Raises:
      - `PreemptRefused` if the holder is a training/HPO/batch tenant (never evicted, FR-155).
      - `SwapError` if the holder is unreachable or the lease never frees.

    Cases (contracts/preempt-flag.md):
      - no holder, or holder is already the target → **no swap** (the normal forward just serves).
      - holder is a serving tenant ≠ target → `unload-now` → wait for the lease to free → return.
    """
    state_fn = state_fn or _default_state
    http_post = http_post or _default_post
    sleep = sleep or asyncio.sleep

    state = await state_fn()
    holder = state.get("holder")
    if not holder or not state.get("resident"):
        return {"swapped": False, "evicted": None, "reason": "no resident holder"}
    if holder == target_label:
        return {"swapped": False, "evicted": None, "reason": "holder is already the target"}
    if holder in NON_PREEMPTABLE:
        raise PreemptRefused("training in progress — not preemptable")

    url = SERVING_HOLDER_URLS.get(holder)
    if url is None:
        # An unknown/serving holder we don't have an unload-now URL for — refuse rather than guess and
        # unload the wrong tenant (spec Edge Case: never unload the wrong holder).
        raise PreemptRefused(f"holder {holder!r} is not a swappable serving tenant")

    await _unload_holder(holder, url, http_post, drain_timeout_s)
    await _wait_for_free(target_label, state_fn, sleep, free_wait_s)
    return {"swapped": True, "evicted": holder, "reason": "ok"}


async def preempt_or_409(target_label: str, **kwargs) -> dict:
    """Router-facing wrapper: run the swap, translating a refusal/failure into a FastAPI `HTTPException`
    (409) so the orchestration core (`preempt_if_needed`) stays framework-free + unit-testable. A
    training holder and a failed/unreachable swap both surface as a clean 409 with an actionable detail."""
    from fastapi import HTTPException

    try:
        return await preempt_if_needed(target_label, **kwargs)
    except PreemptRefused as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SwapError as e:
        raise HTTPException(status_code=409, detail=f"swap failed: {e}")


async def _unload_holder(holder: str, url: str, http_post, drain_timeout_s: float) -> dict:
    """Send the holder's supervisor `unload-now` (drain → unload → release the lease). A non-200 or an
    unreachable holder is a SwapError (don't proceed to forward onto a still-occupied GPU)."""
    try:
        status, body = await http_post(f"{url}/unload-now", {"drain_timeout_s": drain_timeout_s})
    except Exception as e:  # noqa: BLE001 — any transport error means we couldn't evict
        raise SwapError(f"could not reach {holder} supervisor to unload-now: {e}") from e
    if status != 200:
        raise SwapError(f"{holder} unload-now failed ({status}): {str(body)[:200]}")
    result = body if isinstance(body, dict) else {}
    # The supervisor returns 200 even when it COULDN'T evict (e.g. the in-process vision model refuses a
    # hard-cut to preserve one-model-in-VRAM → status "busy"). Only "unloaded"/"idle" mean the GPU is
    # free; anything else must be a SwapError so we never forward the target onto a still-occupied GPU.
    if result.get("status") not in ("unloaded", "idle"):
        raise SwapError(f"{holder} did not unload: {result.get('detail') or result}")
    return result


async def _wait_for_free(target_label: str, state_fn, sleep, free_wait_s: float) -> None:
    """Poll the lease state until it is free for the target — the holder cleared, or the target itself is
    now the holder (a concurrent swap landed it). Sequential by construction: we only forward (and thus
    acquire) once the GPU is actually free (FR-157/158)."""
    waited, step = 0.0, 0.25
    while waited <= free_wait_s:
        state = await state_fn()
        holder = state.get("holder")
        if not holder or not state.get("resident") or holder == target_label:
            return
        await sleep(step)
        waited += step
    raise SwapError(f"lease did not free within {free_wait_s}s after unload-now")


async def _default_state() -> dict:
    """Live holder state from the serving layer (reads a supervisor /health → the shared lease holder)."""
    from . import serving
    return await serving.gpu_state()


async def _default_post(url: str, json_body: dict):
    """POST a control call to a serving supervisor; returns (status_code, parsed_json_or_text)."""
    import httpx

    async with httpx.AsyncClient(timeout=SWAP_FREE_WAIT_S + 5) as client:
        r = await client.post(url, json=json_body)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
