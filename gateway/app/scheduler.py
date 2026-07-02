"""The policy scheduler — the loop, closed (018 US3, T368/T370 — FR-180..183).

A gateway-lifespan asyncio task (research R5: the gateway is the always-on decider; the trainer/
agent executes) ticking a **sync, seam-injected core** so the whole loop is offline-testable with
a fake clock (house style). Per enabled policy, one tick:

  1. **Pending retry** (FR-182): a parked retrain due for retry is re-launched; still busy →
     backoff grows; a success clears the park and stamps the shared cooldown.
  2. **Due checks** (FR-180): monitors run through the existing drift/quality paths; every tick
     writes a durable per-model status + metrics — no silent ticks.
  3. **On breach** (FR-181): reserve the shared cooldown (T347's reserve-before-launch), resolve
     the dataset (`latest` → newest registered version), launch the **breached modality's**
     retrain; GPU busy → park exactly one PendingRetrain (durable, restart-resumable).
  4. **Watches** (FR-183): a policy-launched run that succeeds is gate-checked; `suggest` opens a
     PromotionSuggestion (gate + latest shadow verdict), `auto-on-green` promotes through the
     SAME gated choke-point and writes an audit record; gate warn/blocked makes auto fall back to
     suggest — automation never overrides the gate. `manual` does nothing (today's behavior).
"""
import asyncio
import os
import time

from prometheus_client import Counter

from platformlib.topology import trainer_url

from . import policies, quality

TICK_S = float(os.getenv("POLICY_TICK_S", "30"))
BACKOFF_BASE_S = float(os.getenv("POLICY_RETRY_BASE_S", "60"))
BACKOFF_MAX_S = float(os.getenv("POLICY_RETRY_MAX_S", "1800"))

CHECKS = Counter("gateway_policy_checks_total", "Policy monitor checks", ["result"])
POLICY_RETRAINS = Counter("gateway_policy_retrains_total", "Policy-launched retrains",
                          ["signal", "result"])
PROMOTIONS = Counter("gateway_policy_promotions_total", "Policy promotion outcomes", ["mode"])

#: trainable modality → the task/metric key the quality window scorer uses (011/013 registry).
MODALITY_TASK = {"llm": "text-generation", "vision": "image-classification",
                 "embeddings": "embedding", "asr": "asr"}


class Busy(Exception):
    """The trainer/agent refused the launch with a 409 (GPU busy) — park, never drop (FR-182)."""


class PolicyScheduler:
    def __init__(self, *, store=policies, wall=time.time, check_fn=None, launch_fn=None,
                 watch_fn=None, gate_fn=None, shadow_fn=None, promote_fn=None,
                 reserve_fn=None, release_fn=None, note_fn=None):
        self.store = store
        self.wall = wall
        self.check_fn = check_fn or _default_check
        self.launch_fn = launch_fn or _default_launch
        self.watch_fn = watch_fn or _default_watch
        self.gate_fn = gate_fn or _default_gate
        self.shadow_fn = shadow_fn or _default_shadow
        self.promote_fn = promote_fn or _default_promote
        self.reserve_fn = reserve_fn or quality.try_reserve_retrain
        self.release_fn = release_fn or quality.release_retrain
        self.note_fn = note_fn or quality.note_retrain

    # -- the tick (sync core — fully offline-testable) --------------------------------------------
    def tick(self, now: float = None) -> list:
        now = self.wall() if now is None else now
        actions = []
        try:
            docs = self.store.list_policies()
        except Exception as e:
            CHECKS.labels(result="store_error").inc()
            return [{"action": "store_error", "error": str(e)}]
        for policy in docs:
            if not policy.get("enabled", True):
                continue
            try:
                actions.extend(self._retry_pending(policy, now))
                actions.extend(self._run_due_checks(policy, now))
                actions.extend(self._poll_watch(policy, now))
            except Exception as e:  # one policy must never starve the others
                CHECKS.labels(result="error").inc()
                actions.append({"action": "policy_error", "model": policy.get("model_name"),
                                "error": str(e)})
        return actions

    # -- 1. parked retrain retry (FR-182) ----------------------------------------------------------
    def _retry_pending(self, policy: dict, now: float) -> list:
        model = policy["model_name"]
        pending = self.store.get_pending(model)
        if not pending or now < pending.get("next_attempt_at", 0):
            return []
        try:
            run = self._launch(policy, pending["breach"])
        except Busy:
            pending["attempts"] = pending.get("attempts", 0) + 1
            pending["next_attempt_at"] = now + min(
                BACKOFF_BASE_S * (2 ** pending["attempts"]), BACKOFF_MAX_S)
            self.store.save_pending(pending)
            POLICY_RETRAINS.labels(signal=pending["breach"].get("signal", "?"),
                                   result="still_busy").inc()
            return [{"action": "retry_parked", "model": model,
                     "attempts": pending["attempts"]}]
        except Exception as e:
            # Mirror the initial-launch failure path (Codex review, 018): drop the park and
            # release the shared cooldown so other policies can breach; the next due check on
            # this model re-detects and re-parks. Holding the reservation while re-erroring
            # every tick would block every retrain platform-wide on one broken trainer call.
            self.store.clear_pending(model)
            self.release_fn()
            POLICY_RETRAINS.labels(signal=pending["breach"].get("signal", "?"),
                                   result="failed").inc()
            return [{"action": "retry_failed", "model": model, "error": str(e)}]
        self.store.clear_pending(model)
        self.note_fn()  # the retry finally landed — refresh the shared cooldown stamp
        return [{"action": "retrain_launched", "model": model, "run": run, "retried": True}]

    # -- 2/3. due checks + breach handling (FR-180/181) --------------------------------------------
    def _run_due_checks(self, policy: dict, now: float) -> list:
        model = policy["model_name"]
        status = self.store.get_status(model) or {}
        if now - status.get("last_check_at", 0) < policy["check_interval_s"]:
            return []
        results, breach = [], None
        for monitor in policy.get("monitors", []):
            try:
                res = self.check_fn(policy, monitor)
                CHECKS.labels(result="breach" if res.get("breached") else "ok").inc()
            except Exception as e:
                res = {"kind": monitor.get("kind"), "error": str(e)}
                CHECKS.labels(result="check_error").inc()
            results.append({**res, "kind": monitor.get("kind")})
            if res.get("breached") and breach is None:
                breach = {"signal": monitor.get("kind"), "score": res.get("score"), "at": now}
        # MERGE into the existing status (Codex round 2, 018): a fresh dict here deleted a live
        # `watch` whenever a policy-launched run outlasted one check interval — the completed
        # candidate would then never be evaluated for suggestion/auto-promotion.
        self.store.save_status(model, {**status, "last_check_at": now, "results": results,
                                       "next_due_at": now + policy["check_interval_s"]})
        if breach is None:
            return [{"action": "checked", "model": model, "breached": False}]
        return [{"action": "checked", "model": model, "breached": True},
                *self._handle_breach(policy, breach, now)]

    def _handle_breach(self, policy: dict, breach: dict, now: float) -> list:
        model = policy["model_name"]
        pending = self.store.get_pending(model)
        if pending:  # queue-of-one: a newer breach supersedes the parked one (FR-182)
            pending["breach"] = breach
            self.store.save_pending(pending)
            return [{"action": "breach_superseded_pending", "model": model}]
        if not self.reserve_fn():
            POLICY_RETRAINS.labels(signal=breach["signal"], result="cooldown").inc()
            return [{"action": "breach_in_cooldown", "model": model}]
        try:
            run = self._launch(policy, breach)
        except Busy:
            # keep the reservation — the park IS the reservation's owner until launch succeeds
            self.store.save_pending({"model_name": model, "breach": breach, "attempts": 1,
                                     "next_attempt_at": now + BACKOFF_BASE_S})
            POLICY_RETRAINS.labels(signal=breach["signal"], result="parked").inc()
            return [{"action": "retrain_parked", "model": model}]
        except Exception as e:
            self.release_fn()  # a failed launch must not consume the cooldown (FR-163 semantics)
            POLICY_RETRAINS.labels(signal=breach["signal"], result="failed").inc()
            return [{"action": "retrain_failed", "model": model, "error": str(e)}]
        return [{"action": "retrain_launched", "model": model, "run": run}]

    def _launch(self, policy: dict, breach: dict) -> dict:
        params = dict((policy.get("on_breach") or {}).get("params") or {})
        dataset_name = params.get("dataset_name")
        version = self.store.resolve_dataset_version(
            dataset_name, (policy.get("on_breach") or {}).get("dataset", "latest"))
        run = self.launch_fn(policy, dataset_name, version)
        POLICY_RETRAINS.labels(signal=breach.get("signal", "?"), result="launched").inc()
        run_id = (run or {}).get("run_id") or (run or {}).get("id")
        # The watch persist is BEST-EFFORT after an accepted launch (Codex round 2, 018): a
        # transient store error here must not bubble into the callers' failure paths — that
        # released the cooldown while the run was actually queued (duplicate retrains) and
        # mis-reported an accepted launch as failed. Degraded mode: the run proceeds, only the
        # suggestion/auto step for it is skipped — visible via the metric + action flag.
        if run_id:
            try:
                self.store.save_status(policy["model_name"], {
                    **(self.store.get_status(policy["model_name"]) or {}),
                    "watch": {"run_id": run_id, "launched_at": self.wall()}})
            except Exception:
                CHECKS.labels(result="watch_persist_error").inc()
                return {**(run or {}), "watch_persisted": False}
        return run

    # -- 4. post-run promotion evaluation (FR-183) --------------------------------------------------
    def _poll_watch(self, policy: dict, now: float) -> list:
        model = policy["model_name"]
        status = self.store.get_status(model) or {}
        watch = status.get("watch")
        if not watch:
            return []
        rec = self.watch_fn(watch["run_id"])
        state = (rec or {}).get("status")
        # "unknown" = the trainer could not be polled (down/restarting) — retryable, never a
        # terminal failure that clears the watch (Codex round 2, 018).
        if state in (None, "unknown", "queued", "running", "pending"):
            return []

        def _clear_watch():
            # Cleared only AFTER terminal handling succeeds (Codex review, 018): a transient
            # gate/shadow/store failure below propagates to the tick's per-policy catch with the
            # watch intact, so the successful candidate is retried next tick — never lost.
            status.pop("watch", None)
            self.store.save_status(model, status)

        if state not in ("completed", "succeeded"):
            _clear_watch()
            return [{"action": "watched_run_failed", "model": model, "run_status": state}]
        version = _extract_version(rec)
        if not version:
            _clear_watch()
            return [{"action": "watched_run_no_version", "model": model}]
        mode = policy.get("promotion_mode", "manual")
        if mode == "manual":
            _clear_watch()
            return [{"action": "run_succeeded_manual", "model": model, "version": version}]
        gate_verdict = self.gate_fn(model, version)
        shadow_verdict = self.shadow_fn(model, version, policy.get("modality"))
        green = gate_verdict.get("verdict") == "pass" and (
            shadow_verdict is None or shadow_verdict.get("winner") in ("challenger", "tie"))
        if mode == "auto-on-green" and green:
            promoted = self.promote_fn(model, version)
            if (promoted or {}).get("promoted"):
                self.store.create_suggestion(model, version, gate_verdict, shadow_verdict,
                                             state="auto-promoted", actor=f"policy:{model}")
                PROMOTIONS.labels(mode="auto").inc()
                _clear_watch()
                return [{"action": "auto_promoted", "model": model, "version": version}]
            # The gated choke-point refused (e.g. the incumbent changed since our gate read) —
            # never write an auto-promoted audit for an alias that did not move (Codex review,
            # 018). Fall through to an open suggestion carrying the refusal's verdict.
            gate_verdict = (promoted or {}).get("verdict") or gate_verdict
        # `suggest`, or auto falling back on a non-green/refused verdict — automation never
        # overrides the gate (FR-183).
        self.store.create_suggestion(model, version, gate_verdict, shadow_verdict)
        PROMOTIONS.labels(mode="suggest").inc()
        _clear_watch()
        return [{"action": "suggested", "model": model, "version": version,
                 "gate": gate_verdict.get("verdict")}]

    # -- the lifespan runner ------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event, tick_s: float = TICK_S) -> None:
        from fastapi.concurrency import run_in_threadpool

        while not stop.is_set():
            try:
                await run_in_threadpool(self.tick)
            except Exception:
                pass  # tick() already fails per-policy; this is the belt for the loop itself
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_s)
            except asyncio.TimeoutError:
                continue


# --- default seams (lazy imports; tests inject fakes) ----------------------------------------------

def _extract_version(rec: dict):
    """The trainer's GET /train/{id} success record carries the registered version at top-level
    `model.version` (trainer.py rec: {status, model: {name, version, source}, metrics, ...});
    `result.*` fallbacks cover the agent's journalled JobRecord shape (Codex review, 018)."""
    model = (rec or {}).get("model") or {}
    if isinstance(model, dict) and model.get("version"):
        return str(model["version"])
    result = (rec or {}).get("result") or {}
    inner = result.get("model") or {}
    return (result.get("version") or result.get("registered_version")
            or (inner.get("version") if isinstance(inner, dict) else None)
            or (result.get("registry") or {}).get("version"))


def _default_check(policy: dict, monitor: dict) -> dict:
    from . import monitoring, registry

    if monitor["kind"] == "quality":
        served = registry.get_serving(policy["model_name"])
        if not served:
            return {"breached": False, "detail": "no @serving version to monitor"}
        report = quality.compute_quality(
            policy["model_name"], served["version"], MODALITY_TASK[policy["modality"]],
            window_n=int(monitor.get("window_n", quality.WINDOW_N)),
            drop_pct=float(monitor.get("drop_pct", quality.DROP_PCT)),
            baseline=monitor.get("baseline"))
        return {"breached": bool(report.get("breach")), "score": report.get("value"),
                "report_id": report.get("report_id")}
    ref = monitor["reference"]
    cur_name = monitor.get("current_name") or (policy["on_breach"]["params"]["dataset_name"])
    cur_version = policies.resolve_dataset_version(cur_name, "latest")
    report = monitoring.compute_drift(ref["name"], ref["version"], cur_name, cur_version,
                                      float(monitor.get("threshold",
                                                        monitoring.DEFAULT_THRESHOLD)))
    return {"breached": bool(report.get("dataset_drift")), "score": report.get("max_psi"),
            "report_id": report.get("report_id")}


def _default_launch(policy: dict, dataset_name: str, dataset_version: str) -> dict:
    import httpx

    body = {**((policy.get("on_breach") or {}).get("params") or {}),
            "modality": policy["modality"], "dataset_name": dataset_name,
            "dataset_version": dataset_version}
    body.setdefault("output_name", f"{policy['model_name']}-auto")
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{trainer_url()}/train", json=body)
    if r.status_code == 409:
        raise Busy(r.text[:200])
    if r.status_code not in (200, 202):
        raise RuntimeError(f"trainer returned {r.status_code}: {r.text[:200]}")
    return r.json()


def _default_watch(run_id: str) -> dict:
    import httpx

    with httpx.Client(timeout=10) as client:
        r = client.get(f"{trainer_url()}/train/{run_id}")
    return r.json() if r.status_code == 200 else {"status": "unknown"}


def _default_gate(model: str, version: str) -> dict:
    from . import evaluation

    return evaluation.gate(model, version)


def _default_shadow(model: str, version: str, modality: str):
    """Newest stored shadow verdict for THIS model whose challenger IS this candidate version
    (None if none — a missing window never blocks, per FR-183's 'when one exists').

    Versions are per-model in MLflow, so matching on the version string alone would let model
    A's replay verdict gate model B's promotion (Codex review, 018). Verdicts carry `name`
    since 018 (shadow.build_verdict); older name-less verdicts are skipped, never mis-attributed."""
    try:
        s3 = quality._s3()
        keys = quality._list_keys(s3, quality.SHADOW_PREFIX)
        best, best_id = None, ""
        for k in keys:
            v = quality._get_json(k)
            if (v.get("winner") and v.get("name") == model
                    and str((v.get("challenger") or {}).get("version")) == str(version)
                    and (modality is None or v.get("modality") is None
                         or v.get("modality") == MODALITY_TASK.get(modality, modality))
                    and v.get("shadow_id", "") > best_id):
                best, best_id = v, v.get("shadow_id", "")
        return best
    except Exception:
        return None  # advisory input only — never fail promotion evaluation over it


def _default_promote(model: str, version: str) -> dict:
    from . import registry

    return registry.promote(model, version)  # the SAME gated choke-point — no back door
