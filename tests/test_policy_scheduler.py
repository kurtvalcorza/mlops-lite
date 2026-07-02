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

from _quality import FakeS3  # noqa: E402

from app import policies, scheduler  # noqa: E402


class Cooldown:
    """A fake of quality's shared reserve/release/note cooldown gate."""

    def __init__(self):
        self.reserved = False
        self.notes = 0

    def reserve(self):
        if self.reserved:
            return False
        self.reserved = True
        return True

    def release(self):
        self.reserved = False

    def note(self):
        self.notes += 1


def _setup(*, check_results=None, busy=False):
    fake = FakeS3()
    fake.delete_object = lambda Bucket, Key: fake.objs.pop(Key, None)
    policies._s3 = lambda: fake

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
        assert len(launches) == 1 and cool.notes == 1      # landed once, cooldown stamped
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
        reserve_fn=lambda: True, release_fn=lambda: None, note_fn=lambda: None)
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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
