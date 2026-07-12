"""023 US5 (T519, FR-309..311, SC-158) — failure-injection + restart reconciliation.

Simulates a gateway crash after EVERY activation step by driving the machine partially, then
running `reconcile_all()` as the restarted gateway's lifespan pass would. Asserts convergence to
the verified target (or the previous identity) with NO duplicate reload — the agent fake replays
a completed operation exactly like hostagent/swap.py's per-process operation store — and that
prediction identity is always the AGENT-reported resident, never the pointer or an incomplete
operation's target.
"""
import os
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _activation import (  # noqa: E402
    FakeActivationStore,
    FakeRegistry,
    FakeServing,
    make_service,
)

TARGET_OK = {"status": "reloaded", "load_ms": 42.0, "model_name": "ops-bot",
             "registry_version": "2"}
RESIDENT_OK = {"serving_model": "ops-bot", "serving_version": "2", "resident": True}


def _env(reload_results=None, resident=None, **reg_kw):
    store = FakeActivationStore()
    reg = FakeRegistry(**reg_kw)
    srv = FakeServing(reload_results or [TARGET_OK], resident=resident or RESIDENT_OK)
    act, svc = make_service(store, reg, srv)
    return act, svc, store, reg, srv


def _force_state(store, op_id, state):
    """Simulate the crash: the durable record says `state`, nothing after it happened."""
    store.ops[op_id]["state"] = state


# --- crash after each step, then the restarted gateway reconciles -----------------------------------

def test_crash_after_create_resumes_to_active():
    act, svc, store, reg, srv = _env()
    op = svc.submit(name="ops-bot", version="2")               # crash: still `prepared`
    summary = svc.reconcile_all()                              # the startup pass (T524)
    assert summary == {"resumed": 1, "terminal": 1}
    assert store.ops[op["operation_id"]]["state"] == "active"
    assert reg.pointer_writes == ["ops-bot"] and srv.real_reloads == 1


def test_crash_after_pointer_write_resumes_without_duplicate_reload():
    act, svc, store, reg, srv = _env()
    op = svc.submit(name="ops-bot", version="2")
    _force_state(store, op["operation_id"], "committing")      # pointer landed, then crash
    svc.reconcile_all()
    assert store.ops[op["operation_id"]]["state"] == "active"
    assert srv.real_reloads == 1                               # exactly one reload, ever


def test_crash_after_reload_accepted_replays_idempotently():
    """The reload was ISSUED (agent completed it) but the gateway died before verifying: the
    reconciler re-issues the keyed command; the agent replays the stored result — no second
    unload/reload (FR-307/309)."""
    act, svc, store, reg, srv = _env()
    op = svc.submit(name="ops-bot", version="2")
    oid = op["operation_id"]
    # the agent completed the operation in its process:
    srv.request_llm_reload(operation_id=oid, target={"model_name": "ops-bot", "version": "2"})
    assert srv.real_reloads == 1
    _force_state(store, oid, "reloading")                      # gateway crashed pre-verify
    svc.reconcile_all()
    final = store.ops[oid]
    assert final["state"] == "active"
    assert srv.real_reloads == 1                               # replay, not a duplicate
    assert final["evidence"]["reload"].get("replayed") is True


def test_crash_mid_rollback_resumes_to_rolled_back():
    act, svc, store, reg, srv = _env()
    op = svc.submit(name="ops-bot", version="2", prior={"model_name": "qwen"})
    _force_state(store, op["operation_id"], "rolling_back")    # crash between restore attempts
    svc.reconcile_all()
    assert store.ops[op["operation_id"]]["state"] == "rolled_back"
    assert reg.restores == [{"model_name": "qwen"}]


def test_reconcile_converges_deferral_once_the_holder_clears():
    """Deferred by a job holder at promote time; the job ends; the periodic pass converges."""
    act, svc, store, reg, srv = _env(
        reload_results=[{"status": "deferred", "reason": "job holder — never preempted"}])
    op = svc.submit(name="ops-bot", version="2")
    assert svc.run(op["operation_id"])["state"] == "reloading"  # deferred, pointer kept
    srv.reload_results = [TARGET_OK]                            # the holder finished
    svc.reconcile_all()
    assert store.ops[op["operation_id"]]["state"] == "active"


def test_agent_outage_bounds_attempts_then_degrades_visibly():
    act, svc, store, reg, srv = _env(
        reload_results=[{"status": "unreachable", "reason": "agent unreachable"}])
    op = svc.submit(name="ops-bot", version="2")
    for _ in range(act.MAX_ATTEMPTS + 2):
        svc.reconcile_all()
    final = store.ops[op["operation_id"]]
    assert final["state"] == "degraded"                        # never silent, never fake-active
    assert final["attempts"] == act.MAX_ATTEMPTS


def test_reconcile_advances_via_authorities_not_the_last_call():
    """Contract §Failure: reconciliation READS the authorities. Here the reload reply was lost
    entirely (no stored agent result), but pointer + resident already match the target — the
    re-issued keyed reload no-ops at the agent (already resident) and the op goes active."""
    act, svc, store, reg, srv = _env(
        reload_results=[{"status": "noop", "load_ms": 0.0, "model_name": "ops-bot",
                         "registry_version": "2"}])
    op = svc.submit(name="ops-bot", version="2")
    _force_state(store, op["operation_id"], "reloading")
    store.pointer = {"model_name": "ops-bot", "selected_at": 1.0, "selected_by": "operator"}
    svc.reconcile_all()
    assert store.ops[op["operation_id"]]["state"] == "active"


# --- US5-F1 (review): the operator's preemption confirmation survives a crash ---------------------

class _PreemptGatedServing(FakeServing):
    """A cross-tenant holder that refuses without the operator's preemption confirmation and
    completes WITH it — the case reconcile used to lose (drove preempt=False → permanent defer)."""

    def request_llm_reload(self, preempt=False, *, operation_id=None, target=None):
        self.reload_calls.append({"preempt": preempt, "operation_id": operation_id})
        if operation_id and operation_id in self._completed:
            return {**self._completed[operation_id], "replayed": True}
        if not preempt:
            return {"status": "deferred",
                    "reason": "would displace the resident serving engine — confirm preemption"}
        res = {"status": "reloaded", "model_name": "ops-bot", "registry_version": "2"}
        if operation_id:
            self._completed[operation_id] = res
        self.real_reloads += 1
        return dict(res)


def test_reconcile_preserves_operator_preemption_confirmation():
    """US5-F1: a crash mid-activation of an operator-CONFIRMED preemption must RESUME with the
    confirmation. The confirmation is persisted at submit; reconcile_all re-issues the reload with
    preempt=true and converges to `active`, instead of driving preempt=false → permanent defer →
    degraded (which would defeat US5's recoverability guarantee for exactly the preemption case)."""
    store, reg = FakeActivationStore(), FakeRegistry()
    srv = _PreemptGatedServing(resident=RESIDENT_OK)
    act, svc = make_service(store, reg, srv)

    op = svc.submit(name="ops-bot", version="2", preempt=True)     # operator confirmed preemption
    assert store.ops[op["operation_id"]]["evidence"]["requires_preemption"] is True
    _force_state(store, op["operation_id"], "reloading")           # crash after pointer, pre-verify

    svc.reconcile_all()                                            # the restarted gateway's pass

    assert store.ops[op["operation_id"]]["state"] == "active"      # recovered, not degraded
    assert any(c["preempt"] for c in srv.reload_calls)             # drove WITH the confirmation


def test_reconcile_without_confirmation_still_defers_then_degrades():
    """The negative control: an UNCONFIRMED activation against the same holder must NOT self-grant
    preemption on reconcile — it defers and (bounded) degrades, never silently displaces a holder."""
    store, reg = FakeActivationStore(), FakeRegistry()
    srv = _PreemptGatedServing(resident=RESIDENT_OK)
    act, svc = make_service(store, reg, srv)

    op = svc.submit(name="ops-bot", version="2")                   # preempt defaults False
    assert store.ops[op["operation_id"]]["evidence"]["requires_preemption"] is False
    _force_state(store, op["operation_id"], "reloading")
    for _ in range(act.MAX_ATTEMPTS + 1):
        svc.reconcile_all()
    assert store.ops[op["operation_id"]]["state"] == "degraded"
    assert all(not c["preempt"] for c in srv.reload_calls)         # never self-granted preemption


# --- US5-F2 (review): concurrent drive of one operation issues a single reload ---------------------

def test_concurrent_run_of_one_operation_issues_a_single_reload():
    """US5-F2: the initial activate()'s run() and the periodic reconciler's run() of the SAME
    operation are serialized process-wide, so the agent's keyed reload is issued once even when the
    load is slow (> the reconcile interval). Without the lock both would call request_llm_reload
    before either stored a result → the agent re-executes an in-flight op → a duplicate reload."""
    store, reg = FakeActivationStore(), FakeRegistry()
    in_reload = threading.Event()
    release = threading.Event()

    class _SlowServing(FakeServing):
        def request_llm_reload(self, preempt=False, *, operation_id=None, target=None):
            in_reload.set()
            release.wait(2.0)                                      # a slow (>tick) load, held open
            return super().request_llm_reload(preempt=preempt, operation_id=operation_id,
                                              target=target)

    srv = _SlowServing([TARGET_OK], resident=RESIDENT_OK)
    act, svc = make_service(store, reg, srv)
    op = svc.submit(name="ops-bot", version="2")
    _force_state(store, op["operation_id"], "reloading")

    results = []

    def drive():
        results.append(svc.run(op["operation_id"]))

    t1 = threading.Thread(target=drive)
    t1.start()
    assert in_reload.wait(2.0)                                     # t1 is inside the (locked) reload
    t2 = threading.Thread(target=drive)                           # the reconciler, same op
    t2.start()
    release.set()                                                 # let the slow reload finish
    t1.join(3.0)
    t2.join(3.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert srv.real_reloads == 1                                  # serialized: exactly one reload
    assert store.ops[op["operation_id"]]["state"] == "active"


# --- SC-158: prediction identity is resident-based ----------------------------------------------------

def test_prediction_identity_follows_the_agent_during_incomplete_activation():
    """Mid-activation (reloading, target ops-bot@2) the agent still serves qwen@1: the read model
    must attribute predictions to qwen@1 — desired state never leaks into identity (FR-311/312)."""
    act, svc, store, reg, srv = _env(
        reload_results=[{"status": "deferred", "reason": "job holder"}],
        resident={"serving_model": "qwen", "serving_version": "1", "resident": True})
    op = svc.submit(name="ops-bot", version="2")
    svc.run(op["operation_id"])
    view = svc.read_model()
    assert view["activation"]["state"] == "reloading"
    assert view["resident"] == {"model_name": "qwen", "version": "1", "resident": True}
    assert view["consistent"] is False                          # honest divergence, visible


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
