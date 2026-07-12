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


class TargetUnresolvable(PreemptRefused):
    """The serving-LLM reload target failed its pre-evict load probe — a missing/corrupt artifact or
    an otherwise unresolvable active pointer (FR-265). A `PreemptRefused` subclass so the existing
    409 mapping is unchanged, but distinct so the reload route can TAG it: unlike a retryable
    job-holder/confirm deferral, an unresolvable target means the alias moved to something the next
    cold load would 503 on, so the gateway rolls the active-serving-LLM pointer back — keeping the
    served LLM unchanged (FR-265) not just for the immediate reload but for good."""


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


def reload_serving_llm(manager, *, preempt: bool = False, batch_active_fn=None,
                       drain_timeout_s: float = 10.0) -> dict:
    """Make the ACTIVE serving-LLM live now (022 T466, FR-255 — contracts/serving-resolution.md).

    Admission tenancy is per-ENGINE, so a served-LLM switch has two cases:

      - **cross-tenant** (a non-LLM engine holds the GPU): `preempt_for` — evict → free → load,
        with the job-holder refusal and the target-probe it already carries;
      - **same-tenant model switch** (the `llm` engine is resident with a DIFFERENT model — the
        common US1 case, where `preempt_for` is a satisfied no-op): an explicit force-reload,
        unload the llm child → `ensure_loaded()`, so `llama-server` re-spawns with the newly
        resolved base+adapter. Reusing preempt_for alone here silently keeps serving the old
        GGUF (spec review PR #64 §1).

    Sequencing guards, in order: fresh resolve (rebind(force=True) busts the adapter's TTL) →
    target-probe (`available()`, so a bad artifact never evicts a working holder, FR-256/257) →
    idempotent no-op when the resolved model+version is already resident → job/batch holders are
    NEVER displaced (FR-259) → any displacement of a resident serving model requires the
    operator's confirm (`preempt=true`, set by the console's ConfirmDialog — FR-258). Both paths
    are strictly sequential evict→load under admission: never two models resident (SC-147).
    """
    rt = manager.runtimes.get("llm")
    if rt is None:
        raise PreemptRefused("no llm engine registered")
    adapter = rt.adapter
    # Snapshot the CURRENT binding before rebind() overwrites it — the same-tenant rollback target
    # (the model actually serving now). rebind(force=True) then binds the newly resolved target.
    prior_binding = adapter.snapshot_binding()
    adapter.rebind(force=True)
    ok, reason = adapter.available()
    if not ok:  # probe BEFORE any unload/evict — the working holder stays put (FR-265)
        raise TargetUnresolvable(f"serving-LLM target not loadable: {reason}")
    holder = manager.admission.holder()
    if holder is None:
        return {"status": "loaded", "load_ms": rt.ensure_loaded(),
                **_identity(adapter)}
    if holder["tenant"] != "llm":
        if holder["kind"] in NON_PREEMPTABLE_KINDS:
            raise PreemptRefused(
                f"{holder['tenant']} is running a job (training/HPO/batch) — never preempted; "
                f"the switch is deferred to the next load (FR-259)")
        if not preempt:
            raise PreemptRefused(
                f"would displace resident {holder['tenant']} — operator confirmation required "
                f"(re-issue with preempt=true)")
        res = preempt_for(manager, "llm", drain_timeout_s, batch_active_fn=batch_active_fn)
        return {"status": "swapped", "evicted": res["evicted"], "load_ms": res["load_ms"],
                **_identity(adapter)}
    # same-tenant: the llm engine itself holds the slot
    if adapter.loaded_identity() == adapter.bound_identity():
        # Idempotent no-op FIRST (FR-256): the resolved model+version is already resident, so there
        # is nothing to unload/reload — return noop even under a job/batch holder rather than a
        # spurious refusal for a re-promote of the version that is already live.
        return {"status": "noop", "load_ms": 0.0, **_identity(adapter)}
    if holder["kind"] in NON_PREEMPTABLE_KINDS:
        raise PreemptRefused("the llm slot is held by a job — never preempted (FR-259)")
    if batch_active_fn is not None and batch_active_fn():
        raise PreemptRefused("a GPU batch is driving the llm engine — not reloaded (FR-155)")
    if not preempt:
        loaded = adapter.loaded_identity()
        raise PreemptRefused(
            f"would displace the resident LLM {loaded[0] if loaded else '?'} — operator "
            f"confirmation required (re-issue with preempt=true)")
    result = rt.unload(drain_timeout_s=drain_timeout_s)
    if result.get("status") not in ("unloaded", "idle"):
        raise SwapError(f"llm did not unload for the model switch: "
                        f"{result.get('detail') or result}")
    try:
        load_ms = rt.ensure_loaded()
    except BaseException:
        # available() only file-checks; the target can still fail at llama-server spawn/readiness
        # (corrupt GGUF, OOM, port). The old working model is already evicted, so RESTORE the prior
        # binding and best-effort reload it — serving is never left empty (data-model.md edge case),
        # mirroring preempt_for's cross-tenant rollback. ensure_loaded() already freed the slot on
        # its failure. Propagate the original error so the promote surfaces the switch as failed.
        adapter.restore_binding(prior_binding)
        try:
            rt.ensure_loaded()
        except Exception:
            pass  # GPU stays free; the next request cold-loads on demand
        raise
    return {"status": "reloaded", "load_ms": load_ms, **_identity(adapter)}


def _identity(adapter) -> dict:
    """The bound (now loading/loaded) serving identity for the reload response."""
    name, version = adapter.bound_identity()
    return {"model_name": name, "registry_version": version}


# --- 023 US5 (T523, FR-307/308/312): the reload command keyed by operation_id -----------------------
#
# WRAPS reload_serving_llm — the pre-evict probe (TargetUnresolvable), the idempotent same-target
# no-op, and both swap paths above are NOT re-implemented (contract §Prior art). What is new here:
# a per-process operation store (same operation + same target replays the stored result with no
# second reload; same operation + a DIFFERENT target is rejected), and exact resident verification
# against the gateway-supplied target. Durable cross-process truth stays in Postgres on the
# gateway side (ActivationOperation); this store only needs to cover retries within the agent's
# process lifetime (contract §Agent reload).

_OPS_LOCK_ATTR = "_reload_ops_lock"
_OPS_ATTR = "_reload_operations"


class OperationTargetMismatch(PreemptRefused):
    """The operation_id was previously issued for a DIFFERENT target — a stale/duplicated client
    must never flip the served model to whatever it happens to name now (FR-308). 409."""


def _ops(manager):
    import threading
    lock = getattr(manager, _OPS_LOCK_ATTR, None)
    if lock is None:
        lock = threading.Lock()
        setattr(manager, _OPS_LOCK_ATTR, lock)
        setattr(manager, _OPS_ATTR, {})
    return lock, getattr(manager, _OPS_ATTR)


def reload_serving_llm_op(manager, *, operation_id=None, target=None, preempt=False,
                          batch_active_fn=None, drain_timeout_s: float = 10.0) -> dict:
    """`reload_serving_llm` with operation semantics. Without an `operation_id` this IS the plain
    022 reload (byte-compatible). With one:

      - a stored completed result for (operation_id, same target) returns AS-IS — no second
        unload/reload, however the first attempt ended up resolving (FR-307);
      - (operation_id, different target) raises OperationTargetMismatch → 409 (FR-308);
      - a successful reload is accepted only when the loaded identity IS the requested target
        (FR-312) — a resolver/alias race surfaces as `verify_failed`, never a silent wrong model;
      - a FAILED attempt is not stored, so a retry re-executes (the probe/no-op inside
        reload_serving_llm keep that retry cheap and idempotent)."""
    if not operation_id:
        return reload_serving_llm(manager, preempt=preempt, batch_active_fn=batch_active_fn,
                                  drain_timeout_s=drain_timeout_s)
    want = ((target or {}).get("model_name") or None,
            str((target or {}).get("version")) if (target or {}).get("version") is not None
            else None)
    lock, ops = _ops(manager)
    with lock:
        rec = ops.get(operation_id)
        if rec is not None and rec["target"] != want:
            raise OperationTargetMismatch(
                f"operation {operation_id} was issued for "
                f"{rec['target'][0]}@{rec['target'][1]}, not {want[0]}@{want[1]} — rejected "
                f"(FR-308)")
        if rec is not None and rec.get("result") is not None:
            return {**rec["result"], "replayed": True}
        ops[operation_id] = {"target": want, "result": None}
    result = reload_serving_llm(manager, preempt=preempt, batch_active_fn=batch_active_fn,
                                drain_timeout_s=drain_timeout_s)
    # Exact resident verification (FR-312): success is only success when the agent now serves the
    # very model+version the operation names. `noop` included — a stale operation must not claim
    # active against a different resident.
    if want[0] is not None:
        got = (result.get("model_name"),
               str(result.get("registry_version"))
               if result.get("registry_version") is not None else None)
        if got != want:
            return {**result, "status": "verify_failed",
                    "error": f"resident is {got[0]}@{got[1]}, operation targets "
                             f"{want[0]}@{want[1]}"}  # NOT stored — a later retry re-executes
    with lock:
        ops[operation_id]["result"] = result
    return result
