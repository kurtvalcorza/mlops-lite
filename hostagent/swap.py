"""Transactional preemptive swap (018 US2, FR-171/172) — evict → free → load with no window.

017's gateway-brokered swap released the lease and then raced everyone to re-acquire it — a
third tenant could snipe the freed GPU between evict and target-load (review §4.6). Here the
swap holds an admission **reservation** for the whole transaction: while it is up, `acquire()`
admits only the target, so between eviction and the target's claim there is by construction no
window a contender can win.

Why a reservation and not `admission.lock` held across the transaction (the first design):
every other path takes an engine's runtime lock BEFORE the admission lock (`ensure_loaded`,
the reaper's `idle_reap` → release), so a swap holding the admission lock while calling into
`unload()`/`ensure_loaded()` inverted that order — an ABBA deadlock the moment the reaper
ticked mid-swap (internal review, 018). The reservation closes the same window without ever
holding the admission lock across an engine operation.

Structural guards (FR-172): the holder's `kind` lives in the admission record the agent itself
wrote — a `job` holder (training/HPO/batch) refuses preemption with no network probe and hence
no fail-open path (the guard T346 hardened at the gateway becomes impossible to need here).
"""
from platformlib.topology import NON_PREEMPTABLE_KINDS

from . import admission as adm


class PreemptRefused(Exception):
    """The holder is a job tenant (never preempted) or the target can't take the slot → 409."""


class SwapError(Exception):
    """The holder could not be evicted (wedged / drain refused) → 409 with detail."""


def preempt_for(manager, target_engine_id: str, drain_timeout_s: float = 10.0, *,
                batch_active_fn=None) -> dict:
    """Make room for `target_engine_id` and LOAD it, as one transaction. Returns
    {"swapped": bool, "evicted": tenant|None, "load_ms": float}. The operator confirm and the
    per-request `preempt=true` opt-in semantics are unchanged from 017 (the gateway still fronts
    them); this is only the execution, made atomic.

    `batch_active_fn` (T363, FR-155): a GPU *batch* drives a serving engine WITHOUT taking a
    `kind="job"` admission slot — the engine it feeds holds admission as `kind="serving"`, so the
    `NON_PREEMPTABLE_KINDS` check below would NOT catch it. This callable (the agent passes the
    JobManager's `_gpu_batch_active` view) is read at the SAME decision point as the holder — fresh,
    not a stale pre-captured bool (@claude PR#37) — and refuses evicting the batch-driven serving
    holder: the structural, no-network-probe replacement for the retired gateway swap's fail-closed
    batch probe (the deleted `gateway/app/swap.py`). Decoupled from JobManager by construction."""
    target = manager.runtimes.get(target_engine_id)
    if target is None:
        raise PreemptRefused(f"unknown engine {target_engine_id!r}")
    admission = manager.admission
    try:
        admission.begin_swap(target_engine_id)  # ← the transaction guard (one swap at a time)
    except adm.Held as e:
        raise PreemptRefused(str(e)) from e
    try:
        holder = admission.holder()
        if holder is None or holder["tenant"] == target_engine_id:
            load_ms = target.ensure_loaded()
            return {"swapped": False, "evicted": None, "load_ms": load_ms}
        if holder["kind"] in NON_PREEMPTABLE_KINDS:  # the shared single definition (FR-172)
            raise PreemptRefused(
                f"{holder['tenant']} is running a job (training/HPO/batch) — never preempted")
        # Read the batch flag HERE (same point as the holder read), not a stale pre-captured value:
        # a GPU batch drives this serving holder (FR-155) → never evicted.
        if batch_active_fn is not None and batch_active_fn():
            raise PreemptRefused(
                f"a GPU batch is driving {holder['tenant']} — not preempted (FR-155)")
        holder_rt = manager.runtimes.get(holder["tenant"])
        if holder_rt is None:
            raise PreemptRefused(f"holder {holder['tenant']!r} is not a swappable engine")
        # Probe the target BEFORE evicting (Codex round 5, 018): an unavailable/disabled/wedged
        # target would otherwise evict a working holder and then fail its own load — a bad swap
        # request must not turn into an outage for the resident engine.
        if not target.enabled:
            raise PreemptRefused(f"target {target_engine_id} is disabled")
        if target.wedged_reason:
            raise PreemptRefused(f"target {target_engine_id} is wedged: {target.wedged_reason}")
        ok, reason = target.adapter.available()
        if not ok:
            raise PreemptRefused(f"target {target_engine_id} unavailable: {reason}")
        result = holder_rt.unload(drain_timeout_s=drain_timeout_s)
        if result.get("status") not in ("unloaded", "idle"):
            raise SwapError(f"{holder['tenant']} did not unload: {result.get('detail') or result}")
        try:
            load_ms = target.ensure_loaded()  # the reservation kept the freed slot ours to claim
        except BaseException:
            # Best-effort ROLLBACK (Codex rounds 5+6, 018): the holder is already evicted, and a
            # load failure the probe couldn't see (spawn/readiness) must not leave the GPU empty
            # when the previous engine was healthy. RETARGET the reservation at the holder —
            # never drop it first, or a contender could snipe the freed slot in the gap before
            # the holder's re-acquire (the exact window this transaction exists to close).
            admission.retarget_swap(target_engine_id, holder["tenant"])
            try:
                holder_rt.ensure_loaded()
            except Exception:
                pass  # GPU stays free; the next request cold-loads on demand
            finally:
                admission.end_swap(holder["tenant"])
            raise
        return {"swapped": True, "evicted": holder["tenant"], "load_ms": load_ms}
    finally:
        admission.end_swap(target_engine_id)  # no-op after a rollback retarget
