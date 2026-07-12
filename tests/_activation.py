"""Shared fakes for the activation state-machine suites (023 US5, T518/T519).

`FakeActivationStore` implements the exact accessor surface `gateway/app/activation.py` uses from
`platformlib.store`, in memory, including the one-non-terminal unique-index behavior and CAS
semantics — so the machine's transitions are tested without Postgres. `FakeRegistry`/`FakeServing`
record calls and inject failures per step (the T519 failure matrix).
"""
import time


class FakeConflict(Exception):
    pass


class FakeActivationStore:
    ACTIVATION_NONTERMINAL = ("prepared", "committing", "reloading", "rolling_back")
    ActivationConflict = FakeConflict

    class StoreError(Exception):
        pass

    def __init__(self, *, down=False):
        self.ops = {}
        self.pointer = None       # the ActiveServingLLM row
        self.down = down

    # -- connection surface --------------------------------------------------------------------------

    def connect(self, *a, **kw):
        if self.down:
            raise self.StoreError("store down (injected)")
        return self

    def close(self):
        pass

    # -- pointer (registry reads it through its own seam; kept for read_model) ------------------------

    def get_serving_llm(self, conn):
        return self.pointer

    # -- activation accessors --------------------------------------------------------------------------

    def create_activation(self, conn, *, operation_id, idempotency_key, actor, target_model,
                          target_version, previous_model=None, previous_version=None,
                          evidence=None):
        if any(o["state"] in self.ACTIVATION_NONTERMINAL for o in self.ops.values()):
            raise FakeConflict("one non-terminal activation platform-wide")
        rec = {"operation_id": operation_id, "idempotency_key": idempotency_key,
               "state": "prepared", "actor": actor, "target_model": target_model,
               "target_version": str(target_version), "previous_model": previous_model,
               "previous_version": previous_version, "attempts": 0,
               "created_at": time.time(), "updated_at": time.time(),
               "last_error_code": None, "last_error": None, "evidence": dict(evidence or {})}
        self.ops[operation_id] = rec
        return dict(rec)

    def get_activation(self, conn, operation_id):
        rec = self.ops.get(operation_id)
        return dict(rec) if rec else None

    def find_activation_by_key(self, conn, key):
        matches = [o for o in self.ops.values() if o["idempotency_key"] == key]
        if not matches:
            return None
        matches.sort(key=lambda o: (o["state"] in self.ACTIVATION_NONTERMINAL, o["created_at"]),
                     reverse=True)
        return dict(matches[0])

    def current_activation(self, conn):
        if not self.ops:
            return None
        pending = [o for o in self.ops.values() if o["state"] in self.ACTIVATION_NONTERMINAL]
        pool = pending or list(self.ops.values())
        return dict(max(pool, key=lambda o: o["created_at"]))

    def list_resumable_activations(self, conn):
        return [dict(o) for o in self.ops.values()
                if o["state"] in self.ACTIVATION_NONTERMINAL]

    def cas_activation(self, conn, operation_id, expect_state, new_state, *, error_code=None,
                       error=None, evidence_patch=None, bump_attempts=False):
        expect = [expect_state] if isinstance(expect_state, str) else list(expect_state)
        rec = self.ops.get(operation_id)
        if rec is None or rec["state"] not in expect:
            return None
        rec["state"] = new_state
        rec["updated_at"] = time.time()
        rec["last_error_code"] = error_code
        rec["last_error"] = error
        rec["evidence"].update(evidence_patch or {})
        if bump_attempts:
            rec["attempts"] += 1
        return dict(rec)


class FakeRegistry:
    def __init__(self, *, pointer_write_fails=False, restore_fails=False):
        self.pointer_writes = []
        self.restores = []
        self.pointer_write_fails = pointer_write_fails
        self.restore_fails = restore_fails

    def set_serving_llm(self, name, actor="operator"):
        if self.pointer_write_fails:
            raise RuntimeError("pointer write failed (injected)")
        self.pointer_writes.append(name)
        return {"model_name": name}

    def restore_serving_llm(self, prior):
        self.restores.append(prior)
        if self.restore_fails:
            raise RuntimeError("pointer rollback failed: store down (injected)")


class FakeServing:
    """Scripted agent. `reload_results` is consumed per REAL reload; once an operation completed,
    later same-op calls replay it (mirroring the agent's per-process operation store)."""

    def __init__(self, reload_results=None, resident=None):
        self.reload_results = list(reload_results or [{"status": "reloaded"}])
        self.real_reloads = 0
        self.reload_calls = []
        self._completed = {}
        self.resident_result = resident or {}

    def request_llm_reload(self, preempt=False, *, operation_id=None, target=None):
        self.reload_calls.append({"preempt": preempt, "operation_id": operation_id,
                                  "target": target})
        if operation_id and operation_id in self._completed:
            return {**self._completed[operation_id], "replayed": True}
        result = self.reload_results.pop(0) if len(self.reload_results) > 1 \
            else self.reload_results[0]
        if operation_id and result.get("status") in ("loaded", "reloaded", "swapped", "noop"):
            self._completed[operation_id] = result
        self.real_reloads += 1
        return dict(result)

    def resident_identity(self):
        return dict(self.resident_result)


def make_service(store=None, registry=None, serving=None):
    # gateway/ lands on sys.path through the ONE audited injection point (platformlib.gateway_bridge)
    # — idempotent, so this never stacks duplicate entries (test_gateway_bridge pins that).
    from platformlib.gateway_bridge import _ensure_gateway_on_path
    _ensure_gateway_on_path()
    from app import activation
    return activation, activation.ActivationService(
        store=store, registry=registry, serving=serving, log=lambda *a, **kw: None)
