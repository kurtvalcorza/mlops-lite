"""018 US3 — the policy scheduler with a fake clock (T368, FR-180/181/182).

Offline, GPU-free: every seam injected (store via fake S3, checks, launches, cooldown). Pins:
interval gating (no early checks; every due check writes a durable status — no silent ticks);
breach → modality-correct launch with `latest` resolved at launch time; busy ⇒ queue-of-one
park with growing backoff and supersede; **restart-resume** (a NEW scheduler over the same store
picks the parked retrain up — the U2 remediation); cooldown reserve/release semantics shared
with the manual triggers.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _quality import install_policy_store  # noqa: E402

from app import policies, scheduler  # noqa: E402


class Cooldown:
    """A fake of quality's shared reserve/release/note cooldown gate. Like the real
    try_reserve_retrain (Codex round 5, 018), reserve() returns a truthy TOKEN and release()
    records what token it was given — pinning WHICH releases are token-guarded (fresh
    reserve→launch windows) vs deliberately unconditional (the park's owned stamp)."""

    def __init__(self):
        self.reserved = False
        self.notes = 0
        self.stamp = 0
        self.releases = []  # the token passed to each release (None = unconditional)

    def reserve(self):
        if self.reserved:
            return False
        self.reserved = True
        self.stamp += 1
        return self.stamp

    def release(self, token=None):
        self.releases.append(token)
        self.reserved = False

    def note(self):
        self.notes += 1


def _setup(*, check_results=None, busy=False):
    install_policy_store(policies)  # US4 T375: policy/pending/status/suggestions ride the relational store

    launches = []
    state = {"busy": busy}

    def launch_fn(policy, dataset_name, dataset_version):
        if state["busy"]:
            raise scheduler.Busy("GPU busy")
        launches.append({"model": policy["model_name"], "modality": policy["modality"],
                         "dataset_name": dataset_name, "dataset_version": dataset_version})
        return {"run_id": f"run-{len(launches)}"}

    checks = check_results if check_results is not None else [{"breached": False}]

    def check_fn(policy, monitor):
        return dict(checks[0])

    cool = Cooldown()
    sched = scheduler.PolicyScheduler(
        store=policies, wall=lambda: 0.0, check_fn=check_fn, launch_fn=launch_fn,
        watch_fn=lambda run_id: {"status": "running"},
        reserve_fn=cool.reserve, release_fn=cool.release, note_fn=cool.note)
    # resolve "latest" without a dataset registry: pin it in the fake store layer
    policies_resolve = policies.resolve_dataset_version

    def fake_resolve(name, version):
        return "LATESTVER" if version == "latest" else version

    policies.resolve_dataset_version = fake_resolve
    sched._orig_resolve = policies_resolve  # restore hook for other tests
    return sched, launches, checks, state, cool


def _policy(model="vision-mobilenet", modality="vision", interval=900, mode="manual"):
    return policies.put_policy(model, {
        "modality": modality,
        "monitors": [{"kind": "quality"}],
        "check_interval_s": interval,
        "on_breach": {"action": "retrain", "dataset": "latest",
                      "params": {"dataset_name": "shapes-live"}},
        "promotion_mode": mode,
    })


def _teardown(sched):
    policies.resolve_dataset_version = sched._orig_resolve


def test_interval_gating_and_status_written():
    sched, launches, checks, state, cool = _setup()
    try:
        _policy(interval=900)
        assert any(a["action"] == "checked" for a in sched.tick(now=1000.0))
        st = policies.get_status("vision-mobilenet")
        assert st["last_check_at"] == 1000.0 and st["next_due_at"] == 1900.0
        assert sched.tick(now=1500.0) == []                # not due — no early re-check
        assert any(a["action"] == "checked" for a in sched.tick(now=1901.0))
    finally:
        _teardown(sched)


def test_breach_launches_correct_modality_on_latest_dataset():
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.42}])
    try:
        _policy(model="asr-whisper", modality="asr")
        actions = sched.tick(now=1000.0)
        assert any(a["action"] == "retrain_launched" for a in actions)
        assert launches == [{"model": "asr-whisper", "modality": "asr",
                             "dataset_name": "shapes-live", "dataset_version": "LATESTVER"}]
        assert cool.reserved is True                       # reserve-before-launch (FR-163)
    finally:
        _teardown(sched)


def test_busy_parks_exactly_one_and_backoff_grows_then_resumes():
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.42}], busy=True)
    try:
        _policy(interval=900)
        acts = sched.tick(now=1000.0)
        assert any(a["action"] == "retrain_parked" for a in acts)
        pending = policies.get_pending("vision-mobilenet")
        assert pending["attempts"] == 1 and pending["next_attempt_at"] == 1000.0 + 60
        assert launches == [] and cool.reserved is True    # the park owns the reservation

        acts = sched.tick(now=1061.0)                      # retry due, still busy → backoff x2
        assert any(a["action"] == "retry_parked" for a in acts)
        p2 = policies.get_pending("vision-mobilenet")
        assert p2["attempts"] == 2 and p2["next_attempt_at"] > 1061.0 + 100

        # a NEWER breach supersedes the park, never queues a second retrain (queue-of-one)
        acts = sched.tick(now=1901.0)
        assert any(a["action"] == "breach_superseded_pending" for a in acts)
        assert policies.get_pending("vision-mobilenet")["breach"]["at"] == 1901.0

        state["busy"] = False                              # GPU frees up
        acts = sched.tick(now=5000.0)
        assert any(a["action"] == "retrain_launched" and a.get("retried") for a in acts)
        assert policies.get_pending("vision-mobilenet") is None
        assert len(launches) == 1 and cool.reserved is True
        # Codex round 4 (018): every busy retry REFRESHES the shared cooldown stamp (the park's
        # keep-alive — the stamp would otherwise age out under long contention and another
        # model's breach could reserve over a still-parked retrain), plus the landing stamp:
        # retries at 1061 and 1901, landing at 5000 → three notes.
        assert cool.notes == 3
    finally:
        _teardown(sched)


def test_restart_resume_picks_up_the_parked_retrain():
    # The U2 remediation: PendingRetrain is durable beside the policies, so a NEW scheduler
    # instance (a gateway restart) resumes the retry from the store alone.
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.42}], busy=True)
    try:
        _policy()
        sched.tick(now=1000.0)                             # parks
        assert policies.get_pending("vision-mobilenet") is not None
    finally:
        _teardown(sched)

    launches2 = []

    def launch_ok(policy, dataset_name, dataset_version):
        launches2.append(policy["model_name"])
        return {"run_id": "run-after-restart"}

    fresh = scheduler.PolicyScheduler(                      # the "restarted" gateway
        store=policies, wall=lambda: 9000.0, check_fn=lambda p, m: {"breached": False},
        launch_fn=launch_ok, watch_fn=lambda rid: {"status": "running"},
        reserve_fn=lambda: True, release_fn=lambda token=None: None, note_fn=lambda: None)
    orig = policies.resolve_dataset_version
    policies.resolve_dataset_version = lambda n, v: "LATESTVER"
    try:
        acts = fresh.tick(now=9000.0)
    finally:
        policies.resolve_dataset_version = orig
    assert launches2 == ["vision-mobilenet"]
    assert any(a["action"] == "retrain_launched" and a.get("retried") for a in acts)
    assert policies.get_pending("vision-mobilenet") is None


def test_retry_nonbusy_failure_drops_park_and_releases_reservation():
    # Codex review (018): a parked retrain whose retry fails for a NON-busy reason (trainer 500)
    # must release the shared cooldown and clear the park — not re-error every tick while holding
    # the reservation hostage platform-wide. The next due check re-detects the breach.
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.42}], busy=True)
    try:
        _policy(interval=900)
        sched.tick(now=1000.0)                             # parks; reservation held by the park
        assert cool.reserved is True

        def _explodes(policy, dataset_name, dataset_version):
            raise RuntimeError("trainer 500")

        sched.launch_fn = _explodes
        acts = sched.tick(now=1061.0)                      # retry due → non-busy failure
        assert any(a["action"] == "retry_failed" for a in acts)
        assert policies.get_pending("vision-mobilenet") is None   # park dropped
        assert cool.reserved is False                             # reservation released
        # deliberately TOKENLESS (Codex round 5, 018): the park owns the stamp via its
        # keep-alive refresh, so the original token is stale — a guarded release would leak
        assert cool.releases == [None]
    finally:
        _teardown(sched)


def test_initial_launch_failure_releases_with_its_own_token():
    # Codex round 5 (018): the fresh reserve→launch→release window passes the token so a failed
    # launch can only undo ITS reservation — never a newer stamp another actor landed meanwhile.
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.9}])
    try:
        _policy(interval=900)

        def _explodes(policy, dataset_name, dataset_version):
            raise RuntimeError("trainer 500")

        sched.launch_fn = _explodes
        acts = sched.tick(now=1000.0)
        assert any(a["action"] == "retrain_failed" for a in acts)
        assert cool.releases == [1]                        # the token from THIS reserve
        assert cool.reserved is False
    finally:
        _teardown(sched)


def test_cooldown_blocks_a_second_model_breach():
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.9}])
    try:
        _policy(model="model-a")
        _policy(model="model-b")
        acts = sched.tick(now=1000.0)
        launched = [a for a in acts if a["action"] == "retrain_launched"]
        blocked = [a for a in acts if a["action"] == "breach_in_cooldown"]
        assert len(launched) == 1 and len(blocked) == 1    # one retrain across ALL signals
    finally:
        _teardown(sched)


def test_due_check_preserves_a_live_watch():
    # Codex round 2 (018): saving check results replaced the whole status doc, deleting the
    # watch of any run longer than one check interval — its candidate was never evaluated.
    sched, launches, checks, state, cool = _setup()
    try:
        _policy(interval=900)
        policies.save_status("vision-mobilenet",
                             {"last_check_at": 0.0,
                              "watch": {"run_id": "run-long", "launched_at": 0.0}})
        sched.tick(now=1000.0)                             # a due check runs
        st = policies.get_status("vision-mobilenet")
        assert st["last_check_at"] == 1000.0
        assert st.get("watch", {}).get("run_id") == "run-long"   # the watch survived the merge
    finally:
        _teardown(sched)


def test_watch_persist_failure_never_reports_a_launched_run_as_failed():
    # Codex round 2 (018): launch accepted + watch persist blips → must stay retrain_launched
    # with the cooldown reservation INTACT (releasing it invited duplicate retrains).
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.9}])
    try:
        _policy()
        orig_save = policies.save_status

        def flaky_save(model, status):
            if "watch" in status:
                raise RuntimeError("store blip")
            return orig_save(model, status)

        policies.save_status = flaky_save
        try:
            acts = sched.tick(now=1000.0)
        finally:
            policies.save_status = orig_save
        launched = [a for a in acts if a["action"] == "retrain_launched"]
        assert launched and launched[0]["run"].get("watch_persisted") is False
        assert not any(a["action"] == "retrain_failed" for a in acts)
        assert len(launches) == 1 and cool.reserved is True     # reservation intact
    finally:
        _teardown(sched)


def test_unpollable_trainer_keeps_the_watch_then_expires_it():
    # Codex round 2 (018): a trainer 502/restart reads as status "unknown" — retryable, never a
    # terminal failure that clears the watch and loses the candidate. Internal review (018): but
    # not retryable FOREVER — a run the trainer lost (restart wiped its table) would pin the
    # watch and defer every future breach; past POLICY_WATCH_UNKNOWN_S the watch expires.
    sched, launches, checks, state, cool = _setup()
    try:
        _policy(interval=10**9)                            # no due checks — isolate the watch
        policies.save_status("vision-mobilenet",
                             {"last_check_at": 0.0,
                              "watch": {"run_id": "run-x", "launched_at": 0.0}})
        sched.watch_fn = lambda rid: {"status": "unknown"}
        acts = sched.tick(now=1000.0)                      # first unknown: stamp + surface
        assert any(a["action"] == "watch_unpollable" for a in acts)
        assert "watch" in policies.get_status("vision-mobilenet")
        assert sched.tick(now=2000.0) == []                # within the window: keep retrying
        assert "watch" in policies.get_status("vision-mobilenet")
        acts = sched.tick(now=1000.0 + scheduler.WATCH_UNKNOWN_TIMEOUT_S + 1)
        assert any(a["action"] == "watch_expired_unknown" for a in acts)
        assert "watch" not in policies.get_status("vision-mobilenet")   # breaches re-trigger now
    finally:
        _teardown(sched)


def test_trainer_answering_again_resets_the_unknown_clock():
    sched, launches, checks, state, cool = _setup()
    try:
        _policy(interval=10**9)
        policies.save_status("vision-mobilenet",
                             {"last_check_at": 0.0,
                              "watch": {"run_id": "run-x", "launched_at": 0.0}})
        sched.watch_fn = lambda rid: {"status": "unknown"}
        sched.tick(now=1000.0)                             # stamps unknown_since
        sched.watch_fn = lambda rid: {"status": "running"}
        sched.tick(now=2000.0)                             # the trainer answered — clock resets
        sched.watch_fn = lambda rid: {"status": "unknown"}
        acts = sched.tick(now=1000.0 + scheduler.WATCH_UNKNOWN_TIMEOUT_S + 1)
        assert any(a["action"] == "watch_unpollable" for a in acts)     # a FRESH stamp, no expiry
        assert "watch" in policies.get_status("vision-mobilenet")
    finally:
        _teardown(sched)


def test_breach_defers_while_a_watch_is_unresolved():
    # Codex round 3 (018): launching on a new breach while a watched run is unresolved would
    # overwrite the watch's run_id before it was ever polled — the finished candidate vanishes.
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.9}])
    try:
        _policy(interval=900)
        policies.save_status("vision-mobilenet",
                             {"last_check_at": 0.0,
                              "watch": {"run_id": "run-unresolved", "launched_at": 0.0}})
        acts = sched.tick(now=1000.0)
        assert any(a["action"] == "breach_deferred_watch" for a in acts)
        assert launches == [] and cool.reserved is False       # no launch, no reservation taken
        st = policies.get_status("vision-mobilenet")
        assert st["watch"]["run_id"] == "run-unresolved"       # the watch survived untouched
    finally:
        _teardown(sched)


def test_accepted_retry_never_relaunches_when_clear_pending_blips():
    # Codex round 3 (018): a store blip on clear_pending after an ACCEPTED retry launch must not
    # leave a still-due park behind — later ticks would launch the same breach twice.
    sched, launches, checks, state, cool = _setup(busy=False,
                                                  check_results=[{"breached": False}])
    try:
        _policy(interval=10**9)
        policies.save_pending({"model_name": "vision-mobilenet",
                               "breach": {"signal": "quality", "score": 0.9, "at": 1.0},
                               "attempts": 1, "next_attempt_at": 500.0})
        orig_clear = policies.clear_pending

        def broken_clear(model):
            raise RuntimeError("store blip")

        policies.clear_pending = broken_clear
        try:
            acts = sched.tick(now=1000.0)                      # retry due → launch accepted
        finally:
            policies.clear_pending = orig_clear
        assert any(a["action"] == "retrain_launched" and a.get("retried") for a in acts)
        assert len(launches) == 1
        pending = policies.get_pending("vision-mobilenet")
        assert pending["next_attempt_at"] >= 10 ** 12          # neutralized, not still-due
        assert pending.get("landed") is True                   # marked CONSUMED, not just parked
        acts = sched.tick(now=2000.0)                          # a later tick must NOT relaunch
        assert len(launches) == 1
        assert not any(a["action"].startswith("retry") for a in acts)
        assert policies.get_pending("vision-mobilenet") is None  # retired once the store healed
    finally:
        _teardown(sched)


def test_landed_park_never_blocks_a_new_breach():
    # Internal review (018): the bare far-future sentinel let a LATER breach "supersede" the
    # consumed park and inherit next_attempt_at=10**12 — that retrain never fired again. A landed
    # park must fall through to a normal reserve+launch, even while the store still can't delete.
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.9}])
    try:
        _policy(interval=900)
        policies.save_pending({"model_name": "vision-mobilenet",
                               "breach": {"signal": "quality", "score": 0.8, "at": 1.0},
                               "attempts": 3, "next_attempt_at": 10 ** 12, "landed": True})
        orig_clear = policies.clear_pending

        def broken_clear(model):                               # the outage persists
            raise RuntimeError("store blip")

        policies.clear_pending = broken_clear
        try:
            acts = sched.tick(now=1000.0)
        finally:
            policies.clear_pending = orig_clear
        assert not any(a["action"] == "breach_superseded_pending" for a in acts)
        assert any(a["action"] == "retrain_launched" for a in acts)    # the NEW breach fired
        assert len(launches) == 1
    finally:
        _teardown(sched)


def test_park_persist_failure_hands_the_reservation_back():
    # Internal review (018): busy launch → park write fails → nothing owns the reservation; the
    # old code leaked it, blocking every retrain platform-wide for the full cooldown.
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.9}], busy=True)
    try:
        _policy(interval=900)
        orig_save = policies.save_pending

        def broken_save(pending):
            raise RuntimeError("store blip")

        policies.save_pending = broken_save
        try:
            acts = sched.tick(now=1000.0)
        finally:
            policies.save_pending = orig_save
        assert any(a["action"] == "retrain_park_failed" for a in acts)
        assert cool.reserved is False                          # handed back, not leaked
        assert cool.releases == [1]                            # token-guarded (Codex round 5)
        assert policies.get_pending("vision-mobilenet") is None
        acts = sched.tick(now=1901.0)                          # next due check parks normally
        assert any(a["action"] == "retrain_parked" for a in acts)
        assert cool.reserved is True and policies.get_pending("vision-mobilenet") is not None
    finally:
        _teardown(sched)


def test_transient_store_error_on_retry_keeps_the_park_and_reservation():
    # Internal review (018): a PolicyStoreError during a parked retry (e.g. resolving `latest`
    # mid-outage) is TRANSIENT — dropping the park + releasing there turned a store blip into a
    # lost retrain. It must defer with backoff and land once the store heals.
    sched, launches, checks, state, cool = _setup(
        check_results=[{"breached": True, "score": 0.9}], busy=True)
    try:
        _policy(interval=900)
        sched.tick(now=1000.0)                                 # parks; reservation held
        working_launch = sched.launch_fn

        def store_down(policy, dataset_name, dataset_version):
            raise policies.PolicyStoreError("minio down")

        sched.launch_fn = store_down
        acts = sched.tick(now=1061.0)
        assert any(a["action"] == "retry_deferred_store_error" for a in acts)
        assert policies.get_pending("vision-mobilenet") is not None   # park kept
        assert cool.reserved is True                                  # reservation kept
        sched.launch_fn = working_launch                       # store heals, GPU frees
        state["busy"] = False
        acts = sched.tick(now=9000.0)
        assert any(a["action"] == "retrain_launched" and a.get("retried") for a in acts)
        assert policies.get_pending("vision-mobilenet") is None
    finally:
        _teardown(sched)


def test_default_check_casts_the_knobs_before_scoring():
    # Codex round 6 (018): validation accepts anything int()/float() accepts, so the check path
    # must CAST — an uncast "0.85" baseline reached is_breach's abs() and turned every due check
    # into a check error; the policy never evaluated.
    from app import quality as appq

    captured = {}
    orig_cur, orig_cq = scheduler._current_serving_version, appq.compute_quality
    scheduler._current_serving_version = lambda model: "3"

    def fake_compute(name, version, task, *, window_n, drop_pct, baseline):
        captured.update(window_n=window_n, drop_pct=drop_pct, baseline=baseline)
        return {"breach": False, "value": 0.9}

    appq.compute_quality = fake_compute
    try:
        res = scheduler._default_check(
            {"model_name": "m", "modality": "llm"},
            {"kind": "quality", "window_n": "50", "drop_pct": "0.2", "baseline": "0.85"})
    finally:
        scheduler._current_serving_version, appq.compute_quality = orig_cur, orig_cq
    assert res["breached"] is False
    assert captured == {"window_n": 50, "drop_pct": 0.2, "baseline": 0.85}  # numbers, not strings


def test_default_watch_maps_poll_failures_to_unknown():
    # Codex round 6 (018): a connection error from the trainer poll used to ESCAPE _default_watch,
    # bypassing the unknown_since/WATCH_UNKNOWN_TIMEOUT_S bound entirely — a trainer outage pinned
    # the watch (and deferred every breach) until a gateway restart.
    orig = os.environ.get("TRAINER_URL")
    os.environ["TRAINER_URL"] = "http://127.0.0.1:9"     # nothing listens — refused instantly
    try:
        assert scheduler._default_watch("run-x") == {"status": "unknown"}
    finally:
        if orig is None:
            os.environ.pop("TRAINER_URL", None)
        else:
            os.environ["TRAINER_URL"] = orig


def test_check_error_is_contained_and_recorded():
    sched, launches, checks, state, cool = _setup()
    try:
        _policy()

        def bad_check(policy, monitor):
            raise RuntimeError("store down")

        sched.check_fn = bad_check
        acts = sched.tick(now=1000.0)
        assert any(a["action"] == "checked" and a["breached"] is False for a in acts)
        st = policies.get_status("vision-mobilenet")
        assert "store down" in str(st["results"])          # the error is durable, not silent
    finally:
        _teardown(sched)


def test_idempotency_key_is_stable_per_retrain_identity():
    """019 US3 (FR-192): the launch key is a deterministic function of the retrain's identity (model,
    modality, dataset, resolved version), so a re-detected breach that resolves the SAME `latest`
    version re-derives the SAME key (the trainer dedupes → no duplicate run), while a newly-registered
    dataset version derives a different key (a fresh run, as intended)."""
    k1 = scheduler._idempotency_key("vision-mobilenet", "vision", "imagenette", "7")
    k2 = scheduler._idempotency_key("vision-mobilenet", "vision", "imagenette", "7")
    assert k1 == k2 and k1                                  # deterministic + non-empty
    assert scheduler._idempotency_key("vision-mobilenet", "vision", "imagenette", "8") != k1  # new ver
    assert scheduler._idempotency_key("other-model", "vision", "imagenette", "7") != k1       # new model


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
