"""013 US3 — quality breach + combine policy (T250 / SC-079).

Offline coverage of the breach decision (relative-to-baseline, direction-aware) and the OR+cooldown
combine policy that debounces the two retrain signals. The retrain *launch* itself (httpx → trainer)
is exercised in the live integration leg; here the gating logic is pinned.
"""
import sys

from _quality import load_quality

q = load_quality()


# --- relative-to-baseline breach, both directions -------------------------------------------------

def test_higher_better_breach_threshold():
    # accuracy: breach when value drops > drop_pct below baseline.
    assert q.is_breach(0.80, 0.95, "higher", 0.10) is True    # 0.80 < 0.95*(1-0.10)=0.855
    assert q.is_breach(0.90, 0.95, "higher", 0.10) is False   # within tolerance
    assert q.is_breach(0.99, 0.95, "higher", 0.10) is False   # improved


def test_lower_better_breach_threshold():
    # WER/perplexity: breach when value RISES > drop_pct above baseline (direction inverts the test).
    assert q.is_breach(0.25, 0.20, "lower", 0.10) is True     # 0.25 > 0.20*(1+0.10)=0.22
    assert q.is_breach(0.21, 0.20, "lower", 0.10) is False    # within tolerance
    assert q.is_breach(0.10, 0.20, "lower", 0.10) is False    # improved


def test_baseline_is_like_for_like_only():
    # the 011 baseline is used ONLY when it's the same metric as the window — else None (no breach), so
    # a lower-better perplexity baseline can't be compared against a higher-better accuracy window.
    assert q.baseline_for({"metric": "accuracy", "value": 0.95}, "accuracy") == 0.95
    assert q.baseline_for({"metric": "perplexity", "value": 15.0}, "accuracy") is None  # mismatch → None
    assert q.baseline_for(None, "accuracy") is None
    # and a None baseline can never produce a breach (would otherwise fabricate one).
    assert q.is_breach(0.8, q.baseline_for({"metric": "perplexity", "value": 15.0}, "accuracy"),
                       "higher", 0.10) is False


def test_no_baseline_or_no_value_never_breaches():
    assert q.is_breach(0.5, None, "higher", 0.10) is False    # nothing to compare against
    assert q.is_breach(None, 0.9, "higher", 0.10) is False    # insufficient data


def test_configurable_drop_pct():
    assert q.is_breach(0.90, 0.95, "higher", 0.01) is True    # tight 1% tolerance bites
    assert q.is_breach(0.90, 0.95, "higher", 0.10) is False   # lax 10% tolerance forgives


# --- OR + cooldown combine policy -----------------------------------------------------------------

def test_cooldown_debounces_a_recent_retrain():
    q.reset_cooldown()
    assert q.in_cooldown() is False                            # nothing fired yet
    q.note_retrain(now=1000.0)
    assert q.in_cooldown(now=1100.0, cooldown_sec=3600) is True   # within the window → debounced
    assert q.in_cooldown(now=5000.0, cooldown_sec=3600) is False  # window elapsed → free again
    q.reset_cooldown()


def test_try_reserve_retrain_is_atomic_one_winner():
    # two concurrent quality checks must not both launch: the first reserve wins, the second is
    # debounced. Since 018 the winner gets a truthy TOKEN (the stamp) for a guarded release.
    q.reset_cooldown()
    assert q.try_reserve_retrain(now=1000.0, cooldown_sec=3600) == 1000.0  # reserves → token
    assert q.try_reserve_retrain(now=1001.0, cooldown_sec=3600) is False   # inside cooldown
    assert q.try_reserve_retrain(now=5000.0, cooldown_sec=3600) == 5000.0  # elapsed → free again
    q.reset_cooldown()


def test_release_retrain_frees_a_reservation_for_retry():
    # if a launch fails, releasing the reservation must let the NEXT genuine breach retry (a trainer-down
    # shouldn't consume the cooldown) — symmetric with the PSI path's note-after-success.
    q.reset_cooldown()
    token = q.try_reserve_retrain(now=1000.0, cooldown_sec=3600)
    assert token                                                          # reserve
    q.release_retrain(token)                                              # launch failed → release
    assert q.try_reserve_retrain(now=1001.0, cooldown_sec=3600)           # free again, retry allowed
    q.reset_cooldown()


def test_release_with_a_stale_token_never_clears_a_newer_stamp():
    # Internal review (018): an unconditional release could clear a stamp SOMEONE ELSE now owns —
    # e.g. the policy scheduler's parked-retrain keep-alive refreshed the window between our
    # reserve and our failed launch's release. The token guard makes that release a no-op;
    # a tokenless release keeps the legacy unconditional behavior.
    q.reset_cooldown()
    token = q.try_reserve_retrain(now=1000.0, cooldown_sec=3600)
    q.note_retrain(now=2000.0)                                 # a newer stamp landed in between
    q.release_retrain(token)                                   # stale token → must NOT clear it
    assert q.try_reserve_retrain(now=2001.0, cooldown_sec=3600) is False  # window still live
    q.release_retrain()                                        # tokenless → unconditional
    assert q.try_reserve_retrain(now=2002.0, cooldown_sec=3600)
    q.reset_cooldown()


def test_window_n_must_be_positive():
    # window_n<=0 would make pairs[-0:] the WHOLE history — reject it rather than score everything.
    pairs = [{"prediction": "x", "label": "y"} for _ in range(30)]
    for bad in (0, -5):
        try:
            q.score_window(pairs, "vision", window_n=bad, min_pairs=1)
        except q.QualityError:
            pass
        else:
            raise AssertionError(f"expected QualityError for window_n={bad}")


def test_cooldown_is_shared_across_signals():
    # the policy is OR + cooldown: a retrain from EITHER signal starts the same clock, so the other
    # signal is debounced. note_retrain() is the single shared gate both triggers call.
    q.reset_cooldown()
    q.note_retrain(now=500.0)                                  # e.g. the input-PSI signal fired
    assert q.in_cooldown(now=600.0, cooldown_sec=1000) is True # the quality signal must now debounce
    q.reset_cooldown()


def test_evaluate_quality_breach_flag_drives_the_trigger():
    # the report's breach flag is what the router gates the retrain on — confirm it reflects the
    # baseline comparison end-to-end.
    bad = [{"prediction": "x", "label": "cat", "ts": i} for i in range(25)]   # 0.0 accuracy
    good = [{"prediction": "cat", "label": "cat", "ts": i} for i in range(25)]  # 1.0 accuracy
    rbad = q.evaluate_quality(bad, modality="vision", model_version="3", baseline=0.95, min_pairs=10)
    rgood = q.evaluate_quality(good, modality="vision", model_version="3", baseline=0.95, min_pairs=10)
    assert rbad["breach"] is True and rgood["breach"] is False


# --- 018 US1 (FR-163): BOTH retrain triggers reserve the cooldown BEFORE launching -----------------
#
# Router-level coverage: pre-018 the PSI path launched first and noted the cooldown after
# ("asymmetric by design"), so two concurrent breaches (PSI+PSI or PSI+quality) could both launch.
# These drive the real router handler with the drift compute + trainer launch mocked.

import asyncio
import os as _os
import time as _time

_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_REPO, _os.path.join(_REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _router():
    from app import quality as appq
    from app.routers import monitor as mon
    return mon, appq


def _drift_body(mon):
    return mon.DriftCheck(
        reference=mon.DatasetRef(name="ref", version="v1"),
        current=mon.DatasetRef(name="cur", version="v2"),
        retrain=mon.RetrainSpec(dataset_name="cur", dataset_version="v2", output_name="retrained"),
    )


def test_psi_reserves_cooldown_before_launch_and_debounces():
    mon, appq = _router()
    appq.reset_cooldown()
    launches = []
    orig_launch, orig_drift = mon._launch_retrain, mon.monitoring.compute_drift
    mon._launch_retrain = lambda spec: (launches.append(spec), {"run_id": "r1"})[1]
    mon.monitoring.compute_drift = lambda *a, **k: {"dataset_drift": True, "max_psi": 9.9}
    try:
        first = asyncio.run(mon.check(_drift_body(mon)))
        second = asyncio.run(mon.check(_drift_body(mon)))   # within the cooldown -> debounced
    finally:
        mon._launch_retrain, mon.monitoring.compute_drift = orig_launch, orig_drift
        appq.reset_cooldown()
    assert first["retrain"] == {"run_id": "r1"} and len(launches) == 1
    assert second["retrain"] == {"skipped": "cooldown"}     # PSI now debounces like quality


def test_psi_failed_launch_releases_the_reservation():
    mon, appq = _router()
    appq.reset_cooldown()
    orig_launch, orig_drift = mon._launch_retrain, mon.monitoring.compute_drift
    mon.monitoring.compute_drift = lambda *a, **k: {"dataset_drift": True, "max_psi": 9.9}

    def _down(spec):
        raise RuntimeError("trainer down")

    mon._launch_retrain = _down
    try:
        failed = asyncio.run(mon.check(_drift_body(mon)))
        assert "error" in failed["retrain"]
        # the failed launch must NOT consume the cooldown - the next genuine breach can retry
        mon._launch_retrain = lambda spec: {"run_id": "r2"}
        retried = asyncio.run(mon.check(_drift_body(mon)))
    finally:
        mon._launch_retrain, mon.monitoring.compute_drift = orig_launch, orig_drift
        appq.reset_cooldown()
    assert retried["retrain"] == {"run_id": "r2"}


def test_concurrent_double_breach_launches_exactly_once():
    # Two checks in flight together (the race FR-163 closes): the launcher is slow, so without
    # reserve-before-launch both would pass the gate and both would fire.
    mon, appq = _router()
    appq.reset_cooldown()
    launches = []
    orig_launch, orig_drift = mon._launch_retrain, mon.monitoring.compute_drift
    mon.monitoring.compute_drift = lambda *a, **k: {"dataset_drift": True, "max_psi": 9.9}

    def _slow(spec):
        _time.sleep(0.2)                 # runs in the threadpool - both checks overlap here
        launches.append(spec)
        return {"run_id": f"r{len(launches)}"}

    mon._launch_retrain = _slow

    async def _both():
        return await asyncio.gather(mon.check(_drift_body(mon)), mon.check(_drift_body(mon)))

    try:
        results = asyncio.run(_both())
    finally:
        mon._launch_retrain, mon.monitoring.compute_drift = orig_launch, orig_drift
        appq.reset_cooldown()
    outcomes = sorted(str(r["retrain"]) for r in results)
    assert len(launches) == 1, f"expected exactly one launch, got {len(launches)}: {outcomes}"
    assert any("skipped" in o for o in outcomes) and any("run_id" in o for o in outcomes)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
