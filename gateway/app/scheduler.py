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
  4. **Watches** (FR-183): a policy-launched run that succeeds is gate-checked. Only a GREEN
     candidate — gate pass AND (no shadow window OR shadow win/tie) — proceeds: `suggest` opens
     the one-click PromotionSuggestion (gate + newest shadow verdict), `auto-on-green` promotes
     through the SAME gated choke-point and writes an audit record. Any non-green outcome (gate
     warn/blocked, shadow loss, or a promote-time refusal) is recorded on the policy status as
     `last_candidate` — visible, but never one-click-promotable; a deliberate promote stays on
     /models/{name}/promote. `manual` does nothing (today's behavior).
"""
import asyncio
import os
import time

from prometheus_client import Counter

# T364: the trainer folded into the host agent; agent_url() is the single endpoint, and the agent
# serves the legacy /train + /train/{id} aliases byte-compatibly (FR-177). Kept as a top-level
# platformlib import (not ..settings) so this scheduler stays standalone-loadable.
from platformlib.topology import agent_url

from . import policies, quality
from .settings import agent_headers

TICK_S = float(os.getenv("POLICY_TICK_S", "30"))
BACKOFF_BASE_S = float(os.getenv("POLICY_RETRY_BASE_S", "60"))
BACKOFF_MAX_S = float(os.getenv("POLICY_RETRY_MAX_S", "1800"))
# How long a watch may stay UNPOLLABLE (trainer down / run unknown) before the scheduler gives
# up on it (internal review, 018): a run the trainer lost — a restart that wiped its table —
# would otherwise pin the watch and defer every future breach on that policy forever.
WATCH_UNKNOWN_TIMEOUT_S = float(os.getenv("POLICY_WATCH_UNKNOWN_S", "3600"))

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
        if not pending:
            return []
        if pending.get("landed"):
            # A neutralized park (its launch LANDED but clear_pending blipped) — retire it now
            # that the store answers again; it must never retry and never block a new breach
            # (internal review, 018: the bare far-future sentinel poisoned supersede forever).
            try:
                self.store.clear_pending(model)
            except Exception:
                pass  # still down — retried next tick; _handle_breach ignores landed parks
            return []
        if now < pending.get("next_attempt_at", 0):
            return []
        try:
            run = self._launch(policy, pending["breach"])
        except Busy:
            pending["attempts"] = pending.get("attempts", 0) + 1
            pending["next_attempt_at"] = now + min(
                BACKOFF_BASE_S * (2 ** pending["attempts"]), BACKOFF_MAX_S)
            self.store.save_pending(pending)
            # Refresh the shared cooldown stamp (Codex round 4, 018): the park owns the
            # platform-wide reservation, but the stamp expires after RETRAIN_COOLDOWN_SEC — under
            # long GPU contention another model's breach could reserve over a still-parked
            # retrain. Each busy retry re-stamps so the park's ownership outlives the window.
            self.note_fn()
            POLICY_RETRAINS.labels(signal=pending["breach"].get("signal", "?"),
                                   result="still_busy").inc()
            return [{"action": "retry_parked", "model": model,
                     "attempts": pending["attempts"]}]
        except policies.PolicyStoreError as e:
            # TRANSIENT store trouble (e.g. resolving `latest` mid-outage) is retryable — keep
            # the park AND the reservation (internal review, 018: dropping both here turned a
            # store blip into a lost retrain). Backoff like busy; the store may heal by then.
            pending["attempts"] = pending.get("attempts", 0) + 1
            pending["next_attempt_at"] = now + min(
                BACKOFF_BASE_S * (2 ** pending["attempts"]), BACKOFF_MAX_S)
            try:
                self.store.save_pending(pending)
            except Exception:
                pass  # park unchanged on disk — still due next tick, which is also fine
            self.note_fn()
            POLICY_RETRAINS.labels(signal=pending["breach"].get("signal", "?"),
                                   result="store_error").inc()
            return [{"action": "retry_deferred_store_error", "model": model, "error": str(e)}]
        except Exception as e:
            # Mirror the initial-launch failure path (Codex review, 018): drop the park and
            # release the shared cooldown so other policies can breach; the next due check on
            # this model re-detects and re-parks. Holding the reservation while re-erroring
            # every tick would block every retrain platform-wide on one broken trainer call.
            # Deliberately TOKENLESS (Codex round 5, 018): the park owns the current stamp via
            # its keep-alive refresh (note_fn on every busy retry), so the original reserve
            # token is long stale — a token-guarded release here would no-op and leak the
            # reservation for the full cooldown. While a park lives, nothing else can stamp
            # (reserve fails during cooldown; both HTTP triggers reserve-first), so the
            # unconditional clear can only ever clear the park's own stamp.
            self.store.clear_pending(model)
            self.release_fn()
            POLICY_RETRAINS.labels(signal=pending["breach"].get("signal", "?"),
                                   result="failed").inc()
            return [{"action": "retry_failed", "model": model, "error": str(e)}]
        # The clear is best-effort AFTER an accepted launch (Codex round 3, 018): if the store
        # blips on the delete, neutralize the park (far-future retry) rather than bubbling — a
        # still-due pending would re-launch the SAME breach once the trainer frees up.
        try:
            self.store.clear_pending(model)
        except Exception:
            try:
                # `landed` marks the park as CONSUMED (internal review, 018): the far-future
                # retry time alone let a LATER breach "supersede" this record and inherit the
                # 10**12 next_attempt_at — a retrain that never fired again. Landed parks are
                # ignored by supersede and retired by the next successful retry pass.
                self.store.save_pending({**pending, "landed": True,
                                         "next_attempt_at": 10 ** 12,
                                         "attempts": pending.get("attempts", 0)})
            except Exception:
                pass  # both writes down — the visible action below still records the launch
            CHECKS.labels(result="pending_clear_error").inc()
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
        # A watched run is still unresolved → defer (Codex round 3, 018): launching now would
        # overwrite the watch's run_id before _poll_watch ever evaluated the finished candidate.
        # The breach is not lost — the next due check re-detects it once the watch resolves.
        if (self.store.get_status(model) or {}).get("watch"):
            return [{"action": "breach_deferred_watch", "model": model}]
        pending = self.store.get_pending(model)
        if pending and not pending.get("landed"):
            # queue-of-one: a newer breach supersedes the LIVE parked one (FR-182). A `landed`
            # park is a consumed launch whose cleanup blipped — superseding it would inherit its
            # neutralized far-future retry time and never fire (internal review, 018), so a
            # landed park falls through to a normal reserve+launch instead.
            pending["breach"] = breach
            self.store.save_pending(pending)
            return [{"action": "breach_superseded_pending", "model": model}]
        token = self.reserve_fn()
        if not token:
            POLICY_RETRAINS.labels(signal=breach["signal"], result="cooldown").inc()
            return [{"action": "breach_in_cooldown", "model": model}]
        try:
            run = self._launch(policy, breach)
        except Busy:
            # keep the reservation — the park IS the reservation's owner until launch succeeds
            try:
                self.store.save_pending({"model_name": model, "breach": breach, "attempts": 1,
                                         "next_attempt_at": now + BACKOFF_BASE_S})
            except Exception as e:
                # No park could be persisted, so nothing owns the reservation — hand it back or
                # every retrain platform-wide waits out the full cooldown for a launch that never
                # happened (internal review, 018). The next due check re-detects this breach.
                self.release_fn(token)
                POLICY_RETRAINS.labels(signal=breach["signal"], result="park_failed").inc()
                return [{"action": "retrain_park_failed", "model": model, "error": str(e)}]
            POLICY_RETRAINS.labels(signal=breach["signal"], result="parked").inc()
            return [{"action": "retrain_parked", "model": model}]
        except Exception as e:
            # A failed launch must not consume the cooldown (FR-163 semantics). Token-guarded
            # (Codex round 5, 018): the cooldown is shared with the HTTP trigger routes, so if
            # a newer reservation landed while our launch was failing, an unconditional release
            # would clear THAT live window and invite duplicate retrains.
            self.release_fn(token)
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
        # terminal failure that clears the watch (Codex round 2, 018). But NOT retryable forever
        # (internal review, 018): a run the trainer no longer knows would pin this watch and
        # defer every future breach on the policy — bound it, then let breaches re-trigger.
        if state in (None, "unknown"):
            first = watch.get("unknown_since")
            if first is None:
                watch["unknown_since"] = now
                status["watch"] = watch
                self.store.save_status(model, status)
                return [{"action": "watch_unpollable", "model": model,
                         "run_id": watch["run_id"]}]
            if now - first > WATCH_UNKNOWN_TIMEOUT_S:
                status.pop("watch", None)
                self.store.save_status(model, status)
                CHECKS.labels(result="watch_expired").inc()
                return [{"action": "watch_expired_unknown", "model": model,
                         "run_id": watch["run_id"]}]
            return []
        if state in ("queued", "running", "pending"):
            if watch.pop("unknown_since", None) is not None:
                status["watch"] = watch
                self.store.save_status(model, status)  # the trainer answered — reset the clock
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

        def _record_not_green(verdict, action):
            # FR-183 (Codex round 3, 018): an OPEN one-click suggestion exists ONLY for a green
            # candidate — gate pass AND (no shadow window OR shadow win/tie). A shadow-losing or
            # gate-warn/blocked candidate must not get a one-click promote (accept re-runs only
            # the gate, not the shadow comparison). The outcome stays visible on the policy
            # status; a deliberate promote remains available via /models/{name}/promote.
            status["last_candidate"] = {"version": version, "gate_verdict": verdict,
                                        "shadow_verdict": shadow_verdict, "at": now}
            PROMOTIONS.labels(mode="not_green").inc()
            _clear_watch()
            return [{"action": action, "model": model, "version": version,
                     "gate": (verdict or {}).get("verdict"),
                     "shadow": (shadow_verdict or {}).get("winner")}]

        if not green:
            return _record_not_green(gate_verdict, "candidate_not_green")
        if mode == "auto-on-green":
            promoted = self.promote_fn(model, version)
            if (promoted or {}).get("promoted"):
                self.store.create_suggestion(model, version, gate_verdict, shadow_verdict,
                                             state="auto-promoted", actor=f"policy:{model}")
                PROMOTIONS.labels(mode="auto").inc()
                _clear_watch()
                return [{"action": "auto_promoted", "model": model, "version": version}]
            # The gated choke-point refused (the incumbent changed since our gate read) — the
            # re-gate verdict is no longer green, so record it; never an audit for an alias
            # that did not move, and never a one-click suggestion for a now-blocked candidate.
            return _record_not_green((promoted or {}).get("verdict") or gate_verdict,
                                     "auto_promote_refused")
        # `suggest` + green: the one-click suggestion FR-183 mandates.
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
    from . import monitoring

    if monitor["kind"] == "quality":
        served_version = _current_serving_version(policy["model_name"])
        if not served_version:
            return {"breached": False, "detail": "no @serving version to monitor"}
        # Cast baseline like the other knobs (Codex round 6, 018): validation accepts anything
        # float() accepts, so an uncast "0.85" would reach is_breach's abs() and turn every due
        # check into a check error — the policy would never evaluate.
        baseline = monitor.get("baseline")
        report = quality.compute_quality(
            policy["model_name"], served_version, MODALITY_TASK[policy["modality"]],
            window_n=int(monitor.get("window_n", quality.WINDOW_N)),
            drop_pct=float(monitor.get("drop_pct", quality.DROP_PCT)),
            baseline=float(baseline) if baseline is not None else None)
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


def _idempotency_key(output_name: str, modality: str, dataset_name, dataset_version) -> str:
    """A stable per-retrain key (019/US3, FR-192): the retrain's IDENTITY — the model, modality, and
    resolved dataset version. A breach re-detected because a launch response was lost (or a park re-
    launched after a store blip) resolves the SAME `latest` version, so it re-derives the SAME key and
    the trainer dedupes it to the in-flight/recent run instead of starting a duplicate. A genuinely new
    breach with a newly-registered dataset version derives a different key → a fresh run, as intended
    (re-training the same model on the same data is idempotent, so deduping it is correct, not a bug)."""
    import hashlib

    raw = "|".join(str(x) for x in (output_name, modality, dataset_name, dataset_version))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _default_launch(policy: dict, dataset_name: str, dataset_version: str) -> dict:
    import httpx

    body = {**((policy.get("on_breach") or {}).get("params") or {}),
            "modality": policy["modality"], "dataset_name": dataset_name,
            "dataset_version": dataset_version}
    # FORCE registration under the policy model (Codex rounds 3+5, 018): setdefault still
    # honored an explicitly different output_name in the stored params, while _poll_watch
    # gates/promotes policy.model_name with only the returned version — per-model version
    # numbers made that pair meaningless (blocked as missing, or promoting an unrelated
    # version). Write-time validation now rejects a mismatch; this is the belt for policies
    # stored before it existed.
    body["output_name"] = policy["model_name"]
    # Make the launch idempotent per retrain (019/US3, FR-192): a lost /train response or a
    # post-launch store-write failure re-issues this exact request; the key lets the trainer return
    # the existing run instead of starting a duplicate on the single GPU. Manual /train callers omit
    # the key and keep today's no-dedup behavior.
    body["idempotency_key"] = _idempotency_key(policy["model_name"], policy["modality"],
                                               dataset_name, dataset_version)
    with httpx.Client(headers=agent_headers(), timeout=15) as client:
        r = client.post(f"{agent_url()}/train", json=body)
    if r.status_code == 409:
        raise Busy(r.text[:200])
    if r.status_code not in (200, 202):
        raise RuntimeError(f"trainer returned {r.status_code}: {r.text[:200]}")
    return r.json()


def _default_watch(run_id: str) -> dict:
    import httpx

    # ANY poll failure — connection refused, timeout, bad body — is "unknown" (Codex round 6,
    # 018): an escaping exception here bypassed the unknown_since / WATCH_UNKNOWN_TIMEOUT_S
    # bound entirely, so a trainer outage pinned the watch (and deferred every future breach)
    # until a gateway restart.
    try:
        with httpx.Client(headers=agent_headers(), timeout=10) as client:
            r = client.get(f"{agent_url()}/train/{run_id}")
        return r.json() if r.status_code == 200 else {"status": "unknown"}
    except Exception:
        return {"status": "unknown"}


def _default_gate(model: str, version: str) -> dict:
    from . import evaluation

    return evaluation.gate(model, version)


def _default_shadow(model: str, version: str, modality: str):
    """Newest stored shadow verdict for THIS model whose challenger IS this candidate version
    (None if none — a missing window never blocks, per FR-183's 'when one exists').

    Versions are per-model in MLflow, so matching on the version string alone would let model
    A's replay verdict gate model B's promotion (Codex review, 018). Verdicts carry `name`
    since 018 (shadow.build_verdict); older name-less verdicts are skipped, never mis-attributed.

    Failure discipline (Codex round 4 / internal review, 018): a LISTING failure RAISES — the
    tick's per-policy containment keeps the watch for a retry next tick, exactly like a transient
    gate failure. Returning None instead read as "no shadow window exists", which is GREEN — a
    transient outage could auto-promote a shadow-LOSING candidate. A single unreadable verdict,
    by contrast, is skipped-and-counted: one corrupt key must not veto the whole scan (or wedge
    this candidate's promotion forever).

    Champion staleness (Codex round 6, 018): a verdict counts only when its recorded champion IS
    the current incumbent — the alias can move between the replay and this evaluation, and a
    candidate that beat OLD v1 must not be green-lit over current v3 on that stale window.
    Verdicts with no recorded champion version (or when nothing serves) can't be checked and
    count as before."""
    incumbent = _current_serving_version(model)  # raises → per-policy containment retries
    s3 = quality._s3()
    # Page the listing ourselves to keep each object's LastModified: shadow_ids are random
    # uuid hex, so "largest id" picked an effectively RANDOM verdict when several replays
    # existed for the same candidate (Codex round 3, 018) — newest-by-time is the contract.
    entries, token = [], None
    while True:
        kw = {"Bucket": quality.RESULTS_BUCKET, "Prefix": quality.SHADOW_PREFIX}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        entries.extend((o["Key"], o.get("LastModified")) for o in page.get("Contents", []))
        token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if not token:
            break
    # Collect the MATCHING verdicts, then pick the newest — decoupled (019/US9, FR-197). Fusing the
    # match with the newest-by-time tie-break in one AND (the prior code) meant that once a matching
    # verdict with a real LastModified was chosen, a genuinely-newer match whose LastModified was
    # absent could never replace it (`modified is not None` failed) — and an all-missing-timestamp set
    # was picked in listing order (effectively random). Real S3 always stamps LastModified, so this
    # bites only a non-conforming store/mock, but selection must be total, not order-dependent.
    matches = []
    for key, modified in entries:
        try:
            v = quality._get_json(key)
        except Exception:
            CHECKS.labels(result="shadow_read_error").inc()
            continue  # skip THIS key only — never discard verdicts already found
        champ = str(((v or {}).get("champion") or {}).get("version") or "") or None
        if (v and v.get("winner") and v.get("name") == model
                and str((v.get("challenger") or {}).get("version")) == str(version)
                and (incumbent is None or champ is None or champ == incumbent)
                and (modality is None or v.get("modality") is None
                     or v.get("modality") == MODALITY_TASK.get(modality, modality))):
            matches.append((modified, key, v))
    if not matches:
        return None
    # Newest by LastModified, via a TOTAL, deterministic key (019/US9, FR-197): a verdict WITH a
    # timestamp always outranks one without (so a real recency signal is never lost to a missing
    # one), newest timestamp wins among those, and the (uuid) object key breaks any tie — including
    # an all-missing-timestamp set, which the prior fused predicate picked in listing order
    # (effectively random). Real S3 always stamps LastModified, so the timestamp-absent branch is a
    # non-conforming-store fallback, not a production path. Sorting `(has_ts, ts, key)` never
    # compares None to a datetime, so it can't raise on a mixed set.
    matches.sort(key=lambda m: (m[0] is not None, m[0], m[1]))
    return matches[-1][2]


def _current_serving_version(model: str):
    """The @serving incumbent's version, or None when nothing serves (seam for offline tests)."""
    from . import registry

    serving = registry.get_serving(model)
    return str(serving["version"]) if serving else None


def _default_promote(model: str, version: str) -> dict:
    from . import registry

    return registry.promote(model, version)  # the SAME gated choke-point — no back door
