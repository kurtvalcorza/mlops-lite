"""Durable, recoverable LLM activation (023 US5, T521 — contracts/promotion-activation.md).

One operator promote spans three authorities that cannot share a transaction: the MLflow alias
(gate verdict + metadata), the Postgres `ActiveServingLLM` pointer (desired), and the host agent
(actual resident model). 022 made each single step correct (probe-before-evict, idempotent no-op,
pointer rollback via `registry.restore_serving_llm`); what a crash/timeout between steps left
behind was only discoverable by a human. The `ActivationOperation` record makes the whole action
converge: every transition is a compare-and-set in Postgres, the agent reload is keyed by
`operation_id` (replayed, never duplicated — hostagent/swap.py `reload_serving_llm_op`), and the
reconciler resumes any non-terminal operation after a restart by READING the authorities, never
by inferring success from the last call.

State machine (data-model.md §ActivationOperation):

    prepared -> committing -> reloading -> active            (terminal success)
                                        -> rolling_back -> rolled_back  (terminal safe failure)
                                                        -> degraded     (visible, operator acts)

`deferred` (job holder / operator confirm required) and `unreachable` (agent down) keep the
operation in `reloading` with the error recorded — FR-259's refuse/defer vocabulary: the pointer
stays moved, the next cold load or reconcile pass converges, and nothing is silently rolled back.

Injectable seams (`_store`, `_registry`, `_serving`) keep the machine unit-testable offline
(tests/test_activation.py drives it with fakes; tests/test_activation_recovery.py injects
failures after every step)."""
import os
import threading
import time
import uuid

from prometheus_client import Counter, Gauge

ACTIVATION_OUTCOMES = Counter("gateway_activation_outcomes_total",
                              "Terminal activation-operation outcomes", ["outcome"])
RECONCILE_DURATION = Gauge("gateway_activation_reconcile_ms",
                           "Duration of the last activation reconcile pass")
NONTERMINAL = Gauge("gateway_activation_nonterminal",
                    "Non-terminal activation operations right now")

#: Reload attempts (initial + reconciles) before a still-unverified operation degrades.
MAX_ATTEMPTS = int(os.getenv("ACTIVATION_MAX_ATTEMPTS", "5"))


def _default_store():
    from platformlib import store
    return store


def _default_registry():
    from . import registry
    return registry


def _default_serving():
    from . import serving
    return serving


class ActivationError(Exception):
    """Submit-time refusal (conflicting idempotency key / another operation in flight) → 409."""


class ActivationService:
    """The durable state machine. One instance per process is fine — all state is in Postgres."""

    def __init__(self, store=None, registry=None, serving=None, log=print):
        self._store = store or _default_store()
        self._registry = registry or _default_registry()
        self._serving = serving or _default_serving()
        self._log = log
        # 023 review (US5-F2): serialize every run() in-process. At most one non-terminal operation
        # exists platform-wide (the 002 partial unique index), so this single lock makes the initial
        # activate()'s run() and the periodic reconciler's run() of the SAME operation mutually
        # exclusive — otherwise both could issue the agent's keyed reload before either stored a
        # result, and swap.reload_serving_llm_op re-executes an in-flight (result-None) op → a
        # duplicate reload / serving blip on a slow (>reconcile-interval) load. One gateway process,
        # so an in-process lock is sufficient; the reconciler tick simply waits and then no-ops.
        self._run_lock = threading.Lock()

    # -- connection helper ---------------------------------------------------------------------------

    def _conn(self):
        return self._store.connect()

    # -- submit (contract §Submit) --------------------------------------------------------------------

    def submit(self, *, name: str, version, actor: str = "operator", prior: dict = None,
               idempotency_key: str = None, preempt: bool = False) -> dict:
        """Create (or return) the durable operation for activating name@version. The caller (the
        gated promote route) has already authenticated the operator, run the gate, resolved the
        target, and moved the alias — this records the recoverable half. Returns the operation;
        raises ActivationError on a same-key-different-target or a concurrent non-terminal
        operation for a different target (contract: 409, never a duplicate reload).

        `preempt` is the operator's preemption confirmation. It is persisted in `evidence`
        (US5-F1) so that reconciliation after a crash re-issues the reload WITH the confirmation —
        otherwise a mid-flight preempting activation degrades on restart (reconcile would drive
        preempt=False → PreemptRefused → degraded) instead of recovering, defeating US5's guarantee."""
        key = idempotency_key or f"promote:{name}@{version}"
        conn = self._conn()
        try:
            existing = self._store.find_activation_by_key(conn, key)
            if existing and existing["state"] in self._store.ACTIVATION_NONTERMINAL:
                if (existing["target_model"], existing["target_version"]) != (name, str(version)):
                    raise ActivationError(
                        f"idempotency key {key!r} is in flight for "
                        f"{existing['target_model']}@{existing['target_version']} — refusing the "
                        f"different target {name}@{version} (409)")
                return existing  # same target → the same operation; run()/reconcile converges it
            try:
                # `prior` is a get_serving_llm() snapshot: name-scoped (the pointer carries no
                # version — each model's @serving alias does), so previous_version is usually None.
                return self._store.create_activation(
                    conn, operation_id=uuid.uuid4().hex, idempotency_key=key, actor=actor,
                    target_model=name, target_version=str(version),
                    previous_model=(prior or {}).get("model_name"),
                    previous_version=str((prior or {}).get("version"))
                    if (prior or {}).get("version") is not None else None,
                    evidence={"submitted_at": time.time(),
                              "requires_preemption": bool(preempt)})
            except self._store.ActivationConflict as e:
                current = self._store.current_activation(conn)
                raise ActivationError(
                    f"another activation is in progress "
                    f"({(current or {}).get('operation_id', '?')} -> "
                    f"{(current or {}).get('target_model', '?')}@"
                    f"{(current or {}).get('target_version', '?')}) — retry when it completes"
                ) from e
        finally:
            conn.close()

    def assert_no_conflict(self, name: str, version) -> None:
        """Pre-flight for the promote route (review, Codex — models.py alias order): raise
        ActivationError if a non-terminal activation for a DIFFERENT target is already in flight, so
        the caller refuses the promote BEFORE moving the MLflow `@serving` alias. Otherwise a
        conflict is only discovered at submit() — AFTER the alias moved — leaving the registry
        naming a version that was never activated (inconsistent with the pointer/resident). A
        concurrent op for the SAME target is fine (submit converges it). Store-unavailable is a
        no-op here: activate() falls back to the untracked single-shot path anyway. A tiny TOCTOU
        window remains (an op created between this check and submit), but submit()'s atomic
        one-non-terminal index still refuses it — this just makes the common case clean."""
        try:
            conn = self._conn()
            try:
                cur = self._store.current_activation(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — store down: the durable path is unavailable regardless
            return
        if (cur and cur["state"] in self._store.ACTIVATION_NONTERMINAL
                and (cur["target_model"], cur["target_version"]) != (name, str(version))):
            raise ActivationError(
                f"another activation is in progress ({cur['operation_id']} -> "
                f"{cur['target_model']}@{cur['target_version']}) — retry when it completes")

    # -- drive ----------------------------------------------------------------------------------------

    def run(self, operation_id: str, *, preempt: bool = False) -> dict:
        """Advance the operation as far as it can go right now; returns the final record. Every
        step is idempotent, so run() after a crash (or a reconcile pass) is the SAME code path as
        the first attempt. Serialized process-wide (US5-F2) so a concurrent activate()+reconcile of
        the same operation cannot double-issue the agent's keyed reload."""
        with self._run_lock:
            conn = self._conn()
            try:
                op = self._store.get_activation(conn, operation_id)
                if op is None:
                    raise ActivationError(f"no activation operation {operation_id}")
                if op["state"] == "prepared":
                    op = self._commit_pointer(conn, op) or self._reread(conn, operation_id)
                if op["state"] == "committing":
                    op = self._advance_to_reloading(conn, op) or self._reread(conn, operation_id)
                if op["state"] == "reloading":
                    op = self._reload_and_verify(conn, op, preempt=preempt) \
                        or self._reread(conn, operation_id)
                if op["state"] == "rolling_back":
                    op = self._roll_back(conn, op) or self._reread(conn, operation_id)
                return op
            finally:
                conn.close()

    def _reread(self, conn, operation_id):
        return self._store.get_activation(conn, operation_id)

    def _commit_pointer(self, conn, op):
        """prepared → committing: persist the desired pointer (idempotent upsert)."""
        try:
            self._registry.set_serving_llm(op["target_model"], actor=op["actor"])
        except Exception as e:  # noqa: BLE001 — pointer write failed; stay prepared, record it
            # US5-F4: never persist raw exception text — a driver error can carry host/IP/port; the
            # bounded error_code is the machine signal, the class name is the human breadcrumb.
            return self._store.cas_activation(
                conn, op["operation_id"], "prepared", "prepared",
                error_code="pointer_write_failed",
                error=f"pointer write failed ({type(e).__name__}) — see gateway logs",
                bump_attempts=True)
        return self._store.cas_activation(
            conn, op["operation_id"], "prepared", "committing",
            evidence_patch={"pointer_set": op["target_model"]})

    def _advance_to_reloading(self, conn, op):
        return self._store.cas_activation(conn, op["operation_id"], "committing", "reloading")

    def _reload_and_verify(self, conn, op, *, preempt: bool = False):
        """reloading → active | rolling_back | (stays reloading on defer/unreachable)."""
        oid = op["operation_id"]
        target = {"model_name": op["target_model"], "version": op["target_version"]}
        reload_res = self._serving.request_llm_reload(
            preempt=preempt, operation_id=oid, target=target)
        status = reload_res.get("status")
        evidence = {"reload": {k: reload_res.get(k) for k in
                               ("status", "load_ms", "model_name", "registry_version",
                                "replayed", "evicted", "reason", "unresolvable", "error")
                               if k in reload_res}}
        if status in ("loaded", "reloaded", "swapped", "noop"):
            # The keyed reload already verified the exact target resident (FR-312, agent-side);
            # confirm against live agent health so `active` is never claimed from a stale reply.
            ident = self._serving.resident_identity()
            evidence["resident"] = {"model": ident.get("serving_model"),
                                    "version": ident.get("serving_version"),
                                    "resident": ident.get("resident")}
            if (ident.get("serving_model") == op["target_model"]
                    and str(ident.get("serving_version")) == op["target_version"]):
                done = self._store.cas_activation(conn, oid, "reloading", "active",
                                                  evidence_patch=evidence)
                if done:
                    ACTIVATION_OUTCOMES.labels(outcome="active").inc()
                return done
            return self._store.cas_activation(
                conn, oid, "reloading", "reloading", error_code="verify_pending",
                error=f"agent reports {ident.get('serving_model')}@"
                      f"{ident.get('serving_version')}, not the target yet",
                evidence_patch=evidence, bump_attempts=True)
        if status == "verify_failed":
            return self._maybe_degrade(conn, op, "verify_failed",
                                       reload_res.get("error") or "target verification failed",
                                       evidence)
        if reload_res.get("unresolvable"):
            # FR-265 (unchanged from 022): an unloadable artifact rolls the pointer back for good.
            moved = self._store.cas_activation(conn, oid, "reloading", "rolling_back",
                                               error_code="unresolvable",
                                               error=reload_res.get("reason") or "",
                                               evidence_patch=evidence)
            if moved:
                return self._roll_back(conn, moved)
            return moved
        # deferred (job holder / confirm required) or unreachable: the pointer stays moved and the
        # switch converges on the next cold load / reconcile pass (FR-259) — bounded by attempts.
        return self._maybe_degrade(
            conn, op, "unreachable" if status == "unreachable" else "deferred",
            reload_res.get("reason") or "", evidence, stay="reloading")

    def _maybe_degrade(self, conn, op, code, reason, evidence, *, stay=None):
        if op["attempts"] + 1 >= MAX_ATTEMPTS:
            done = self._store.cas_activation(
                conn, op["operation_id"], op["state"], "degraded", error_code=code,
                error=f"{reason} (after {op['attempts'] + 1} attempts)"[:400],
                evidence_patch=evidence, bump_attempts=True)
            if done:
                ACTIVATION_OUTCOMES.labels(outcome="degraded").inc()
            return done
        return self._store.cas_activation(
            conn, op["operation_id"], op["state"], stay or op["state"], error_code=code,
            error=str(reason)[:400], evidence_patch=evidence, bump_attempts=True)

    def _roll_back(self, conn, op):
        """rolling_back → rolled_back | degraded: restore the previous pointer (022's
        restore_serving_llm — never a parallel rollback implementation)."""
        prior = None
        if op["previous_model"]:
            prior = {"model_name": op["previous_model"]}
        try:
            self._registry.restore_serving_llm(prior)
        except Exception as e:  # noqa: BLE001 — the double-fault window: pointer may still name
            done = self._store.cas_activation(  # the unloadable target — degraded, visible
                conn, op["operation_id"], "rolling_back", "degraded",
                error_code="pointer_error",  # US5-F4: sanitized — no raw driver text
                error=f"rollback pointer write failed ({type(e).__name__}) — see gateway logs")
            if done:
                ACTIVATION_OUTCOMES.labels(outcome="degraded").inc()
            return done
        done = self._store.cas_activation(
            conn, op["operation_id"], "rolling_back", "rolled_back",
            evidence_patch={"pointer_restored": (prior or {}).get("model_name") or "(cleared)"})
        if done:
            ACTIVATION_OUTCOMES.labels(outcome="rolled_back").inc()
        return done

    # -- the promote route's one-call entry (T522) -----------------------------------------------------

    def activate(self, *, name: str, version, prior: dict, preempt: bool,
                 kind: str = None, base: dict = None) -> dict:
        """Drive one operator activation and return the promote response's `serving_llm` view
        (byte-compatible with 022's shape, plus an `activation` object when the durable record
        exists). When the activation store is unavailable (store down, table not yet migrated),
        falls back to 022's exact single-shot sequence — an operator promote must not fail just
        because durability is degraded; it only loses recoverability, visibly."""
        try:
            op = self.submit(name=name, version=version, actor="operator", prior=prior,
                             preempt=preempt)
        except ActivationError:
            raise  # a REAL conflict (concurrent op / key reuse) → the route's 409
        except Exception as e:  # noqa: BLE001 — no durable store: the 022 single-shot path
            self._log(f"activation: store unavailable ({e.__class__.__name__}) — running the "
                      f"untracked single-shot activation", flush=True)
            return self._untracked(name, prior, preempt, kind, base)
        try:
            final = self.run(op["operation_id"], preempt=preempt)
        except Exception as e:  # noqa: BLE001 — mid-flight store loss: the op is durable; the
            return {"active": name, "kind": kind, "base": base,  # reconciler resumes it (T524)
                    "error": f"activation interrupted ({e.__class__.__name__}) — "
                             f"reconciliation resumes operation {op['operation_id']}",
                    "activation": {"operation_id": op["operation_id"], "state": "unknown"}}
        return self._view(final, name=name, kind=kind, base=base)

    def _untracked(self, name, prior, preempt, kind, base) -> dict:
        """022's promote sequence, verbatim (pointer → reload → unresolvable rollback → double-
        fault pointer_error) — the durable path expresses the same outcomes through the state
        machine; this is the degraded-durability twin, never a divergent behavior."""
        try:
            self._registry.set_serving_llm(name, actor="operator")
        except Exception as e:  # noqa: BLE001 — the pre-023 shape for a failed pointer write
            return {"active": None, "error": str(e)}
        reload_res = self._serving.request_llm_reload(preempt=preempt)
        if reload_res.get("unresolvable"):
            sl = {"active": (prior or {}).get("model_name"), "kind": kind, "base": base,
                  "reload": reload_res}
            try:
                self._registry.restore_serving_llm(prior)
                sl["rolled_back"] = True
            except Exception as re_err:  # noqa: BLE001 — surfaced distinctly (022 double fault)
                sl["rolled_back"] = False
                sl["pointer_error"] = str(re_err)
            return sl
        return {"active": name, "kind": kind, "base": base, "reload": reload_res}

    def _view(self, op, *, name, kind, base) -> dict:
        """Map a (possibly terminal) operation record onto the promote response shape."""
        activation_info = {"operation_id": op["operation_id"], "state": op["state"],
                           "attempts": op["attempts"], "last_error": op["last_error"],
                           "last_error_code": op["last_error_code"]}
        reload_ev = (op.get("evidence") or {}).get("reload") or {}
        if op["state"] == "rolled_back":
            return {"active": op["previous_model"], "kind": kind, "base": base,
                    "reload": reload_ev, "rolled_back": True, "activation": activation_info}
        if op["state"] == "degraded" and op["last_error_code"] == "pointer_error":
            return {"active": op["previous_model"], "kind": kind, "base": base,
                    "reload": reload_ev, "rolled_back": False,
                    "pointer_error": op["last_error"], "activation": activation_info}
        if op["state"] == "prepared":  # the pointer write itself failed (pre-023: active=None)
            return {"active": None, "error": op["last_error"] or "pointer write failed",
                    "activation": activation_info}
        return {"active": name, "kind": kind, "base": base, "reload": reload_ev,
                "activation": activation_info}

    # -- reconcile (contract §Failure and reconciliation) ---------------------------------------------

    def reconcile_all(self) -> dict:
        """Resume every non-terminal operation (startup + periodic, T524). Reads authorities —
        pointer, resident — and re-drives run(); an operation whose authorities already agree
        advances straight to active without touching the agent's load path (the keyed reload
        replays/no-ops)."""
        t0 = time.monotonic()
        conn = self._conn()
        try:
            ops = self._store.list_resumable_activations(conn)
        finally:
            conn.close()
        summary = {"resumed": 0, "terminal": 0}
        for op in ops:
            summary["resumed"] += 1
            try:
                # US5-F1: drive with the operator's PERSISTED preemption confirmation, so resuming a
                # mid-flight preempting activation re-issues the reload with preempt=true rather than
                # degrading on a PreemptRefused it would never clear.
                preempt = bool((op.get("evidence") or {}).get("requires_preemption"))
                final = self.run(op["operation_id"], preempt=preempt)
                if final and final["state"] not in self._store.ACTIVATION_NONTERMINAL:
                    summary["terminal"] += 1
            except Exception as e:  # noqa: BLE001 — one stuck op must not stall the loop
                self._log(f"activation reconcile: {op['operation_id']} failed: "
                          f"{e.__class__.__name__}: {e}", flush=True)
        NONTERMINAL.set(summary["resumed"] - summary["terminal"])
        RECONCILE_DURATION.set(int((time.monotonic() - t0) * 1000))
        return summary

    def pending_count(self) -> int:
        conn = self._conn()
        try:
            return len(self._store.list_resumable_activations(conn))
        finally:
            conn.close()

    # -- read model (contract §Read) --------------------------------------------------------------

    def read_model(self) -> dict:
        """{desired, resident, activation, consistent} — desired/actual confusion is impossible to
        express here by construction: `desired` is the pointer, `resident` is the agent, and
        `consistent` is true only when they NAME the same model in a terminal-success state."""
        desired = {"model_name": None, "version": None}
        activation = None
        try:
            conn = self._conn()
            try:
                rec = self._store.get_serving_llm(conn)
                if rec:
                    desired["model_name"] = rec["model_name"]
                op = self._store.current_activation(conn)
                if op:
                    # US5-F3: fall back to the operation's target when the pointer is unset/unreadable
                    # (e.g. state `prepared`, pointer not yet written). `setdefault` was a no-op here
                    # because "model_name" is always present (initialized to None above).
                    if desired["model_name"] is None:
                        desired["model_name"] = op["target_model"]
                    desired["version"] = op["target_version"] \
                        if op["target_model"] == desired["model_name"] else None
                    activation = {"operation_id": op["operation_id"], "state": op["state"],
                                  "target": f"{op['target_model']}@{op['target_version']}",
                                  "attempts": op["attempts"],
                                  "last_error": op["last_error"],
                                  "last_error_code": op["last_error_code"]}
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — store down: report the honest unknowns below
            pass
        ident = self._serving.resident_identity()
        resident = {"model_name": ident.get("serving_model"),
                    "version": ident.get("serving_version"),
                    "resident": bool(ident.get("resident"))}
        consistent = bool(
            desired["model_name"] and desired["model_name"] == resident["model_name"]
            and (activation is None or activation["state"] in ("active",))
            and (desired["version"] is None or str(desired["version"])
                 == str(resident["version"])))
        return {"desired": desired, "resident": resident, "activation": activation,
                "consistent": consistent}


_service = None


def service() -> ActivationService:
    """The process-wide instance (all state is durable; the instance is just seams + counters)."""
    global _service
    if _service is None:
        _service = ActivationService()
    return _service
