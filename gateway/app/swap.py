"""Swap-on-demand orchestration (017) — operator-confirmed preemptive GPU lease for *serving*.

008 shipped a cooperative **refuse-if-held** lease: a serving request is refused (409) while another
tenant holds the GPU. 017's A2 fast-follow lets an operator, with an explicit **`preempt=true`**, evict a
resident *serving* model so a different serving modality can load — a single sequential swap, **one model
in VRAM at any instant** (Principle II). A running **training/HPO/batch** job is **never** preempted.

This module is the gateway's swap broker (D2): it never holds the lease itself — it identifies the holder,
sends the holder's supervisor an `unload-now` control call, waits for the lease to free, and returns so the
caller's normal forward (which acquires the now-free lease and loads the target) proceeds. The default
(no `preempt`) path never calls in here, so 008 behavior is byte-for-byte unchanged (FR-161/SC-101).

Robustness (review hardening):
  - **Batch never preempted** — a GPU *batch* drives a serving supervisor **without** holding the training
    lease, so the holder reads as `llm`/`vision`/`asr`; the broker asks the trainer whether a GPU batch is
    active and refuses the swap if so (FR-155), on top of the `training` lease-holder refusal. The probe
    **fails closed** (018/FR-162): an unreachable trainer means the batch state is UNKNOWN, so the swap is
    refused with that reason rather than gambling an active batch's work on an eviction.
  - **Verify the target before evicting** — probe the target daemon's health first, so an unreachable
    target never causes us to drop the only working serving model for a request that would 503 anyway.
  - **Re-resolve a stale holder** — if the holder we snapshotted already idle-released (`unload-now` →
    `idle`) we re-read the lease and act on the real current holder instead of assuming the swap is done.
  - **Serialize swaps** — concurrent `preempt=true` swaps are serialized (per event loop) so they don't
    evict each other's freshly-loaded target.

Seams (`state_fn`, `http_post`, `sleep`, `batch_active_fn`, `target_probe_fn`) are injectable so the
orchestration is unit-testable with the lease + daemon HTTP mocked (no GPU, no live daemons).
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

# How many times we re-resolve the holder when `unload-now` reports `idle` (a stale snapshot — the holder
# released between our state read and the control call). Bounds churn if the holder keeps changing.
SWAP_MAX_RERESOLVE = int(os.getenv("SWAP_MAX_RERESOLVE", "3"))

TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")

# Optional gateway-only shared secret for the destructive `unload-now` control call. When set, the gateway
# forwards it as the `X-Swap-Control` header and each supervisor requires it; unset → 008 behavior (the
# supervisors' control surface is gateway-gated on the private WSL network, like /infer).
SWAP_CONTROL_SECRET = os.getenv("SWAP_CONTROL_SECRET", "")

# Holder LABEL (as reported by serving.gpu_state, mapped from the lease tenant) → the supervisor base URL
# that owns it and exposes `unload-now`. Only **serving** tenants are here; "training" is intentionally
# absent — it is never preemptable (FR-155) and the orchestrator refuses before any unload-now.
SERVING_HOLDER_URLS = {
    "llm": os.getenv("SERVING_URL", "http://host.docker.internal:8090"),
    "vision": os.getenv("BENTO_URL", "http://host.docker.internal:8092"),
    "asr": os.getenv("ASR_URL", "http://host.docker.internal:8095"),
}
# Health URL used to verify a swap *target* is reachable before we evict the current holder. The llama /
# whisper supervisors expose `/health`; the BentoML vision service exposes `/healthz` (override-able).
TARGET_HEALTH_URLS = {
    "llm": f"{SERVING_HOLDER_URLS['llm']}/health",
    "asr": f"{SERVING_HOLDER_URLS['asr']}/health",
    "vision": os.getenv("BENTO_HEALTH_URL", f"{SERVING_HOLDER_URLS['vision']}/healthz"),
}
# Tenants that hold the GPU for long-running work and must never be evicted by a swap (FR-155). "training"
# is the shared lease identity for fine-tune / HPO / batch runs (see training/trainer.py, hpo.py).
NON_PREEMPTABLE = {"training"}

# Per-event-loop swap lock: serialize preempt swaps so two concurrent ones don't evict each other's target.
# Keyed by loop so the offline tests (a fresh `asyncio.run` loop per test) each get their own lock rather
# than reusing one bound to a dead loop; in the live gateway (one persistent loop) it is a singleton.
_swap_locks: dict = {}


class PreemptRefused(Exception):
    """A `preempt=true` swap was refused — the holder is a training/HPO/batch tenant (never preempted,
    FR-155) or could not be evicted. The router maps this to a clear 409."""


class SwapError(Exception):
    """The swap could not be completed (holder unreachable, lease never freed). Maps to 409/503."""


def _loop_swap_lock() -> asyncio.Lock:
    loop = asyncio.get_event_loop()
    lock = _swap_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _swap_locks[loop] = lock
    return lock


async def preempt_if_needed(target_label: str, *, state_fn=None, http_post=None, sleep=None,
                            batch_active_fn=None, target_probe_fn=None,
                            drain_timeout_s: float = SWAP_DRAIN_TIMEOUT_S,
                            free_wait_s: float = SWAP_FREE_WAIT_S,
                            max_reresolve: int = SWAP_MAX_RERESOLVE) -> dict:
    """Make room on the GPU for `target_label` when an operator opted into a swap (`preempt=true`).

    Returns a small dict describing what happened (`{"swapped": bool, "evicted": <label|None>, ...}`); the
    caller then runs its **normal forward**, which acquires the now-free lease and loads the target. Raises:
      - `PreemptRefused` if the holder is a training/HPO/batch tenant or a GPU batch is active (FR-155).
      - `SwapError` if the target is unreachable, the holder can't be evicted, or the lease never frees.

    Cases (contracts/preempt-flag.md):
      - no holder, or holder is already the target → **no swap** (the normal forward just serves).
      - holder is a serving tenant ≠ target → verify target reachable → `unload-now` → wait for free.
      - `unload-now` reports `idle` (holder already released) → re-resolve the real holder and retry.
    """
    state_fn = state_fn or _default_state
    http_post = http_post or _default_post
    sleep = sleep or asyncio.sleep
    batch_active_fn = batch_active_fn or _default_batch_active
    target_probe_fn = target_probe_fn or _default_target_probe

    async with _loop_swap_lock():  # serialize the whole evict→free so concurrent preempts don't fight
        for _ in range(max(1, max_reresolve)):
            state = await state_fn()
            holder = state.get("holder")
            # Gate solely on `holder` (the global lease holder, from gpu_lease.current_holder — which
            # already filters out dead holders, so a non-null holder IS a live, evictable tenant; matches
            # data-model.md). The LLM-supervisor `resident` flag is NOT used: it is False whenever a
            # vision/asr tenant holds the lease (Principle II), which would wrongly skip the swap.
            if not holder:
                return {"swapped": False, "evicted": None, "reason": "no holder"}
            if holder == target_label:
                return {"swapped": False, "evicted": None, "reason": "holder is already the target"}
            if holder in NON_PREEMPTABLE:
                raise PreemptRefused("training in progress — not preemptable")
            # A GPU batch drives a serving supervisor without taking the training lease, so its holder is
            # llm/vision/asr — but a running batch is never preempted (FR-155). Refuse before evicting.
            # The seam may return a bool or a truthy reason string (018/FR-162: the default probe returns
            # "batch state unknown …" when the trainer is unreachable — fail-CLOSED, never evict blind).
            batch = await batch_active_fn()
            if batch:
                raise PreemptRefused(batch if isinstance(batch, str)
                                     else "batch inference in progress — not preemptable")

            url = SERVING_HOLDER_URLS.get(holder)
            if url is None:
                # An unknown/serving holder we don't have an unload-now URL for — refuse rather than guess
                # and unload the wrong tenant (spec Edge Case: never unload the wrong holder).
                raise PreemptRefused(f"holder {holder!r} is not a swappable serving tenant")

            # Verify the target daemon is reachable BEFORE evicting the current holder — otherwise a
            # down/unreachable target would drop the only working serving model for a request that then
            # 503s anyway (evict-then-fail). Reachable-but-idle is fine (it loads on demand).
            if not await target_probe_fn(target_label):
                raise SwapError(f"target {target_label!r} is unreachable — not evicting {holder!r}")

            result = await _unload_holder(holder, url, http_post, drain_timeout_s)
            if result.get("status") == "idle":
                # Stale snapshot: the holder we saw already released the lease before our unload-now landed
                # (idle = nothing to unload). Re-resolve — the lease may now be free, or a different tenant
                # may have grabbed it — and act on the real current holder rather than assume we're done.
                continue
            await _wait_for_free(target_label, state_fn, sleep, free_wait_s)
            return {"swapped": True, "evicted": holder, "reason": "ok"}
        raise SwapError("holder kept changing during the swap — could not free the lease")


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
    unreachable holder is a SwapError (don't proceed to forward onto a still-occupied GPU). Returns the
    parsed body so the caller can distinguish `unloaded` (evicted) from `idle` (already released → stale)."""
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
        # Free ⇔ the holder cleared (lease released) or the target already holds it. Gate on `holder`
        # only — same reason as preempt_if_needed: the LLM-specific `resident` flag is False for a live
        # vision/asr holder, which would falsely report "free" before that holder finishes releasing and
        # race the target's forward acquire().
        if not holder or holder == target_label:
            return
        await sleep(step)
        waited += step
    raise SwapError(f"lease did not free within {free_wait_s}s after unload-now")


async def _default_state() -> dict:
    """Live holder state from the serving layer (reads a supervisor /health → the shared lease holder)."""
    from . import serving
    return await serving.gpu_state()


async def _default_batch_active():
    """Whether the trainer is running a **GPU** batch (which drives a serving supervisor without holding the
    training lease). Reads the trainer /health `gpu_batch_active`.

    **Fail-CLOSED** (018 US1, FR-162 — review §4.6): when the batch state cannot be determined (trainer
    unreachable / bad response), return a truthy *reason* so the swap is REFUSED with an explicit
    "batch state unknown" detail. The pre-018 fail-open here was the one path that could evict a serving
    supervisor an active GPU batch was driving — exactly the case FR-155 forbids. The cost of failing
    closed is that preemptive swaps 409 while the trainer daemon is down; the default (non-preempt)
    path is unaffected, and a 409 with a clear reason beats destroying batch work."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{TRAINER_URL}/health")
        if r.status_code == 200:
            return bool(r.json().get("gpu_batch_active"))
        return f"batch state unknown (trainer /health returned {r.status_code}) — refusing preempt (fail-closed)"
    except Exception as e:
        return f"batch state unknown (trainer unreachable: {e}) — refusing preempt (fail-closed)"


async def _default_target_probe(target_label: str) -> bool:
    """Whether the swap *target* daemon is reachable (so we don't evict the holder just to 503). Any HTTP
    response means the daemon is up; only a transport error (process down/unreachable) returns False. An
    unmapped target isn't blocked (best-effort — the forward will surface any error)."""
    url = TARGET_HEALTH_URLS.get(target_label)
    if not url:
        return True
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.get(url)
        return True
    except Exception:
        return False


async def _default_post(url: str, json_body: dict):
    """POST a control call to a serving supervisor; returns (status_code, parsed_json_or_text).

    The HTTP timeout is sized off the **drain bound** (plus the free-wait backstop and a small buffer), not
    just `SWAP_FREE_WAIT_S`: `unload-now` can legitimately take up to `SWAP_DRAIN_TIMEOUT_S` draining an
    in-flight request before it responds, so a timeout tied only to the post-unload wait would spuriously
    report `swap failed` during a normal long request."""
    import httpx

    headers = {"X-Swap-Control": SWAP_CONTROL_SECRET} if SWAP_CONTROL_SECRET else None
    timeout = SWAP_DRAIN_TIMEOUT_S + SWAP_FREE_WAIT_S + 5
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=json_body, headers=headers)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
