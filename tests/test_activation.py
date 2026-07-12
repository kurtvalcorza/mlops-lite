"""023 US5 (T518, FR-305..314) — the activation state machine, pure/offline.

Drives gateway/app/activation.py over the in-memory fakes (tests/_activation.py): every state and
transition, idempotency-key semantics, one-non-terminal serialization, CAS lost-race behavior,
and the read model. The Postgres accessor layer has its own live checks; the FAILURE matrix +
restart reconciliation live in test_activation_recovery.py (T519).
"""
import os
import sys

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


def _happy():
    store = FakeActivationStore()
    reg = FakeRegistry()
    srv = FakeServing([TARGET_OK], resident=RESIDENT_OK)
    act, svc = make_service(store, reg, srv)
    return act, svc, store, reg, srv


# --- assert_no_conflict: refuse a conflicting op BEFORE the caller moves the alias (Codex) --------

def test_assert_no_conflict_refuses_a_different_in_flight_target():
    """The promote route calls this BEFORE registry.promote() moves the @serving alias, so a
    conflict 409s without leaving the registry naming a version that was never activated."""
    act, svc, store, reg, srv = _happy()
    svc.submit(name="ops-bot", version="1")                       # a non-terminal op is now in flight
    with pytest.raises(act.ActivationError):
        svc.assert_no_conflict("other-llm", "5")                  # a DIFFERENT target → refuse early


def test_assert_no_conflict_allows_same_target_and_clear_field():
    act, svc, store, reg, srv = _happy()
    svc.assert_no_conflict("ops-bot", "2")                        # nothing in flight → fine
    op = svc.submit(name="ops-bot", version="2")
    svc.assert_no_conflict("ops-bot", "2")                        # SAME target → submit converges it
    svc.run(op["operation_id"])                                   # drive it terminal
    svc.assert_no_conflict("ops-bot", "2")                        # terminal → no longer a conflict


# --- the full success path ---------------------------------------------------------------------------

def test_submit_run_reaches_active_with_ordered_evidence():
    act, svc, store, reg, srv = _happy()
    op = svc.submit(name="ops-bot", version="2", prior={"model_name": "qwen"})
    assert op["state"] == "prepared"
    assert op["previous_model"] == "qwen"                      # rollback target captured (FR-306)
    final = svc.run(op["operation_id"], preempt=True)
    assert final["state"] == "active"
    assert reg.pointer_writes == ["ops-bot"]                   # committing wrote the pointer once
    call = srv.reload_calls[0]                                 # reload keyed by the operation
    assert call["operation_id"] == op["operation_id"]
    assert call["target"] == {"model_name": "ops-bot", "version": "2"}
    assert call["preempt"] is True                             # operator confirm passes through
    assert final["evidence"]["reload"]["status"] == "reloaded"
    assert final["evidence"]["resident"]["model"] == "ops-bot"  # verified, not assumed (FR-312)


def test_active_requires_agent_reported_resident_target():
    """`active` is CLAIMED only when the agent says the exact target is resident — a pointer or a
    2xx reload reply is never enough (contract §Authorities)."""
    store = FakeActivationStore()
    srv = FakeServing([TARGET_OK],
                      resident={"serving_model": "other", "serving_version": "9",
                                "resident": True})
    act, svc = make_service(store, FakeRegistry(), srv)
    op = svc.submit(name="ops-bot", version="2")
    final = svc.run(op["operation_id"])
    assert final["state"] == "reloading"                       # NOT active
    assert final["last_error_code"] == "verify_pending" and final["attempts"] == 1


# --- idempotency + serialization (FR-307/305) ---------------------------------------------------------

def test_same_key_same_target_returns_the_same_operation():
    act, svc, store, *_ = _happy()
    op1 = svc.submit(name="ops-bot", version="2")
    op2 = svc.submit(name="ops-bot", version="2")
    assert op1["operation_id"] == op2["operation_id"]
    assert len(store.ops) == 1                                 # never a duplicate operation


def test_same_key_different_target_is_refused():
    act, svc, *_ = _happy()
    svc.submit(name="ops-bot", version="2", idempotency_key="k1")
    with pytest.raises(act.ActivationError):
        svc.submit(name="ops-bot", version="3", idempotency_key="k1")


def test_second_concurrent_activation_is_refused():
    act, svc, *_ = _happy()
    svc.submit(name="ops-bot", version="2")
    with pytest.raises(act.ActivationError):                   # one non-terminal platform-wide
        svc.submit(name="other-bot", version="1")


def test_terminal_operation_frees_the_platform_for_the_next():
    act, svc, store, reg, srv = _happy()
    op = svc.submit(name="ops-bot", version="2")
    svc.run(op["operation_id"])
    srv.resident_result = {"serving_model": "other-bot", "serving_version": "1",
                           "resident": True}
    srv.reload_results = [{"status": "reloaded", "model_name": "other-bot",
                           "registry_version": "1"}]
    op2 = svc.submit(name="other-bot", version="1")            # no conflict after terminal
    assert svc.run(op2["operation_id"])["state"] == "active"


# --- refusal/deferral/rollback outcomes ---------------------------------------------------------------

def test_deferral_keeps_pointer_and_stays_reloading():
    """A job-holder / missing-confirm deferral is retryable (FR-259): the pointer stays moved and
    the operation stays in `reloading` for the reconciler — nothing rolls back."""
    store = FakeActivationStore()
    reg = FakeRegistry()
    srv = FakeServing([{"status": "deferred",
                        "reason": "would displace resident vision — confirmation required"}])
    act, svc = make_service(store, reg, srv)
    op = svc.submit(name="ops-bot", version="2")
    final = svc.run(op["operation_id"])
    assert final["state"] == "reloading" and final["last_error_code"] == "deferred"
    assert reg.restores == []


def test_unresolvable_rolls_back_and_terminates():
    store = FakeActivationStore()
    reg = FakeRegistry()
    srv = FakeServing([{"status": "deferred", "unresolvable": True,
                        "reason": "serving-LLM target not loadable: GGUF absent"}])
    act, svc = make_service(store, reg, srv)
    op = svc.submit(name="ops-bot", version="2", prior={"model_name": "qwen"})
    final = svc.run(op["operation_id"])
    assert final["state"] == "rolled_back"
    assert reg.restores == [{"model_name": "qwen"}]            # 022's restore, not a re-derivation


def test_rollback_write_failure_degrades_with_pointer_error():
    store = FakeActivationStore()
    reg = FakeRegistry(restore_fails=True)
    srv = FakeServing([{"status": "deferred", "unresolvable": True, "reason": "not loadable"}])
    act, svc = make_service(store, reg, srv)
    op = svc.submit(name="ops-bot", version="2", prior={"model_name": "qwen"})
    final = svc.run(op["operation_id"])
    assert final["state"] == "degraded"                        # visible, never fake success
    assert final["last_error_code"] == "pointer_error"


def test_pointer_write_failure_stays_prepared_and_recorded():
    store = FakeActivationStore()
    reg = FakeRegistry(pointer_write_fails=True)
    act, svc = make_service(store, reg, FakeServing())
    op = svc.submit(name="ops-bot", version="2")
    final = svc.run(op["operation_id"])
    assert final["state"] == "prepared"
    assert final["last_error_code"] == "pointer_write_failed" and final["attempts"] == 1


def test_repeated_verify_failure_degrades_at_the_attempt_bound():
    store = FakeActivationStore()
    srv = FakeServing([{"status": "verify_failed", "error": "resident is x@1, targets ops-bot@2"}])
    act, svc = make_service(store, FakeRegistry(), srv)
    op = svc.submit(name="ops-bot", version="2")
    final = None
    for _ in range(act.MAX_ATTEMPTS + 1):
        final = svc.run(op["operation_id"])
    assert final["state"] == "degraded" and final["last_error_code"] == "verify_failed"
    assert final["attempts"] == act.MAX_ATTEMPTS               # bounded, not forever (FR-310)


# --- the untracked fallback + read model ---------------------------------------------------------------

def test_store_down_falls_back_to_untracked_single_shot():
    store = FakeActivationStore(down=True)
    reg = FakeRegistry()
    srv = FakeServing([{"status": "reloaded"}])
    act, svc = make_service(store, reg, srv)
    sl = svc.activate(name="ops-bot", version="2", prior=None, preempt=False,
                      kind="full-model", base=None)
    assert sl["active"] == "ops-bot" and sl["reload"]["status"] == "reloaded"
    assert "activation" not in sl                               # honest: nothing durable exists
    assert reg.pointer_writes == ["ops-bot"]
    assert srv.reload_calls[0]["operation_id"] is None          # the plain unkeyed 022 reload


def test_read_model_reports_desired_resident_and_consistency():
    act, svc, store, reg, srv = _happy()
    store.pointer = {"model_name": "ops-bot", "selected_at": 1.0, "selected_by": "operator"}
    op = svc.submit(name="ops-bot", version="2")
    svc.run(op["operation_id"])
    view = svc.read_model()
    assert view["desired"]["model_name"] == "ops-bot"
    assert view["resident"]["model_name"] == "ops-bot"          # agent-reported, never the pointer
    assert view["activation"]["state"] == "active"
    assert view["consistent"] is True
    # now the agent disagrees (restart, different resident): consistent flips, resident is honest
    srv.resident_result = {"serving_model": "qwen", "serving_version": "1", "resident": True}
    view = svc.read_model()
    assert view["resident"]["model_name"] == "qwen" and view["consistent"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
