"""Transactional preemptive swap (018 US2, FR-171/172) — evict → free → load under ONE lock.

017's gateway-brokered swap released the lease and then raced everyone to re-acquire it — a
third tenant could snipe the freed GPU between evict and target-load (review §4.6). Here the
whole transaction runs while holding `admission.lock` (re-entrant): no admission decision can
interleave, so between eviction and the target's claim there is by construction no window.

Structural guards (FR-172): the holder's `kind` lives in the admission record the agent itself
wrote — a `job` holder (training/HPO/batch) refuses preemption with no network probe and hence
no fail-open path (the guard T346 hardened at the gateway becomes impossible to need here).
"""
from . import admission as adm


class PreemptRefused(Exception):
    """The holder is a job tenant (never preempted) or the target can't take the slot → 409."""


class SwapError(Exception):
    """The holder could not be evicted (wedged / drain refused) → 409 with detail."""


def preempt_for(manager, target_engine_id: str, drain_timeout_s: float = 10.0) -> dict:
    """Make room for `target_engine_id` and LOAD it, as one transaction. Returns
    {"swapped": bool, "evicted": tenant|None, "load_ms": float}. The operator confirm and the
    per-request `preempt=true` opt-in semantics are unchanged from 017 (the gateway still fronts
    them); this is only the execution, made atomic."""
    target = manager.runtimes.get(target_engine_id)
    if target is None:
        raise PreemptRefused(f"unknown engine {target_engine_id!r}")
    with manager.admission.lock:  # ← the transaction: nothing else can admit until we return
        holder = manager.admission.holder()
        if holder is None or holder["tenant"] == target_engine_id:
            load_ms = target.ensure_loaded()
            return {"swapped": False, "evicted": None, "load_ms": load_ms}
        if holder["kind"] == "job":
            raise PreemptRefused(
                f"{holder['tenant']} is running a job (training/HPO/batch) — never preempted")
        holder_rt = manager.runtimes.get(holder["tenant"])
        if holder_rt is None:
            raise PreemptRefused(f"holder {holder['tenant']!r} is not a swappable engine")
        result = holder_rt.unload(drain_timeout_s=drain_timeout_s)
        if result.get("status") not in ("unloaded", "idle"):
            raise SwapError(f"{holder['tenant']} did not unload: {result.get('detail') or result}")
        load_ms = target.ensure_loaded()  # the slot is provably free — we never released the lock
        return {"swapped": True, "evicted": holder["tenant"], "load_ms": load_ms}
