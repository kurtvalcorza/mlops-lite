"""018 US3 — promotion modes (T370, FR-183).

Offline, GPU-free: the scheduler's post-run evaluation with every seam injected. Pins:
`manual` = no-op (today's behavior, byte-for-byte); `suggest` opens a PromotionSuggestion
carrying gate + shadow verdicts; `auto-on-green` promotes through the gated choke-point and
writes an `auto-promoted` audit record; gate warn/blocked (or a shadow loss) makes auto FALL
BACK to suggest — automation never overrides the gate; failed runs and versionless results
drop the watch without inventing promotions.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _quality import install_policy_store  # noqa: E402

from app import policies, scheduler  # noqa: E402


def _setup(*, mode, run_status="completed", version="9", gate="pass", shadow=None):
    install_policy_store(policies)  # US4 T375: policy state rides the relational store

    promoted = []
    sched = scheduler.PolicyScheduler(
        store=policies, wall=lambda: 100.0,
        check_fn=lambda p, m: {"breached": False},
        launch_fn=lambda p, dn, dv: {"run_id": "run-x"},
        # the trainer's REAL success record shape: top-level model.version (Codex review, 018)
        watch_fn=lambda rid: {"status": run_status,
                              "model": ({"name": "qa-demo", "version": version}
                                        if version else None)},
        gate_fn=lambda model, ver: {"verdict": gate, "candidate": {"version": ver}},
        shadow_fn=lambda model, ver, modality: shadow,
        promote_fn=lambda model, ver: promoted.append((model, ver)) or {"promoted": True},
        reserve_fn=lambda: True, release_fn=lambda token=None: None, note_fn=lambda: None)

    policies.put_policy("qa-demo", {
        "modality": "llm",
        "monitors": [{"kind": "quality"}],
        "check_interval_s": 900,
        "on_breach": {"action": "retrain", "dataset": "latest",
                      "params": {"dataset_name": "qa-live"}},
        "promotion_mode": mode,
    })
    policies.save_status("qa-demo", {"last_check_at": 100.0,
                                     "watch": {"run_id": "run-x", "launched_at": 50.0}})
    return sched, promoted


def test_manual_mode_does_nothing():
    sched, promoted = _setup(mode="manual")
    acts = sched.tick(now=100.0)
    assert any(a["action"] == "run_succeeded_manual" for a in acts)
    assert promoted == [] and policies.list_suggestions() == []
    assert "watch" not in (policies.get_status("qa-demo") or {})   # watch consumed


def test_suggest_opens_a_suggestion_with_both_verdicts():
    sched, promoted = _setup(mode="suggest", shadow={"winner": "challenger",
                                                     "challenger": {"version": "9"}})
    acts = sched.tick(now=100.0)
    assert any(a["action"] == "suggested" for a in acts)
    assert promoted == []                                          # suggest never promotes
    (rec,) = policies.list_suggestions(state="open")
    assert rec["candidate_version"] == "9"
    assert rec["gate_verdict"]["verdict"] == "pass"
    assert rec["shadow_verdict"]["winner"] == "challenger"


def test_auto_on_green_promotes_via_the_gate_and_audits():
    sched, promoted = _setup(mode="auto-on-green", shadow={"winner": "tie"})
    acts = sched.tick(now=100.0)
    assert any(a["action"] == "auto_promoted" for a in acts)
    assert promoted == [("qa-demo", "9")]                          # the gated choke-point ran
    (rec,) = policies.list_suggestions(state="auto-promoted")
    assert rec["actor"] == "policy:qa-demo" and rec["resolved_at"] is not None


def test_auto_with_no_shadow_window_is_still_green():
    sched, promoted = _setup(mode="auto-on-green", shadow=None)    # "when one exists" (FR-183)
    sched.tick(now=100.0)
    assert promoted == [("qa-demo", "9")]


def test_non_green_candidates_get_no_one_click_suggestion():
    # Codex round 3 (018) / FR-183: accept re-runs only the GATE — an open one-click suggestion
    # for a gate-warn/blocked or shadow-LOSING candidate would bypass the shadow signal. The
    # outcome is recorded on the policy status instead, in BOTH modes.
    for mode in ("suggest", "auto-on-green"):
        for gate, shadow in (("warn", None), ("blocked", None),
                             ("pass", {"winner": "champion"})):    # challenger lost the window
            sched, promoted = _setup(mode=mode, gate=gate, shadow=shadow)
            acts = sched.tick(now=100.0)
            assert any(a["action"] == "candidate_not_green" for a in acts), (mode, gate, shadow)
            assert promoted == []                                  # never overrides the gate
            assert policies.list_suggestions() == []               # nothing one-click-promotable
            st = policies.get_status("qa-demo")
            assert st["last_candidate"]["version"] == "9"          # still visible to the operator
            assert "watch" not in st                               # candidate handled, not lost


def test_auto_refused_by_the_gated_promote_records_not_green():
    # Codex review (018): the gated choke-point can refuse (incumbent changed since our gate
    # read) — never an auto-promoted audit for an alias that did not move, and (round 3) never
    # a one-click suggestion for a now-blocked candidate either.
    sched, promoted = _setup(mode="auto-on-green")
    refusal = {"promoted": False, "verdict": {"verdict": "blocked", "reason": "incumbent moved"}}
    sched.promote_fn = lambda model, ver: refusal
    acts = sched.tick(now=100.0)
    assert any(a["action"] == "auto_promote_refused" for a in acts)
    assert policies.list_suggestions() == []
    st = policies.get_status("qa-demo")
    assert st["last_candidate"]["gate_verdict"]["verdict"] == "blocked"  # refusal verdict kept


def test_transient_failure_keeps_the_watch_for_retry():
    # Codex review (018): the watch is cleared only after terminal handling succeeds — a
    # transient gate failure must not lose the successful candidate.
    sched, promoted = _setup(mode="suggest")
    calls = {"n": 0}

    def flaky_gate(model, ver):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("mlflow hiccup")
        return {"verdict": "pass", "candidate": {"version": ver}}

    sched.gate_fn = flaky_gate
    acts = sched.tick(now=100.0)
    assert any(a["action"] == "policy_error" for a in acts)    # contained, not silent
    assert "watch" in policies.get_status("qa-demo")           # candidate NOT lost
    acts = sched.tick(now=100.0)                               # next tick retries and lands
    assert any(a["action"] == "suggested" for a in acts)
    assert "watch" not in policies.get_status("qa-demo")
    assert policies.list_suggestions(state="open")


class _ListingS3:
    """Fake S3 with per-key LastModified, for the _default_shadow scans."""

    def __init__(self, objs):
        self.objs = objs  # key -> (json_dict, last_modified)

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        return {"Contents": [{"Key": k, "LastModified": lm}
                             for k, (_, lm) in sorted(self.objs.items())
                             if k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        import io
        import json as _json

        return {"Body": io.BytesIO(_json.dumps(self.objs[Key][0]).encode())}


def _with_shadow_env(fake_s3, incumbent, fn):
    """Run fn() with the shadow store faked and the @serving incumbent pinned — _default_shadow
    consults the registry for champion staleness since Codex round 6 (018)."""
    from app import quality as appq

    orig_s3, orig_cur = appq._s3, scheduler._current_serving_version
    appq._s3 = lambda: fake_s3
    scheduler._current_serving_version = lambda model: incumbent
    try:
        return fn()
    finally:
        appq._s3, scheduler._current_serving_version = orig_s3, orig_cur


def test_default_shadow_picks_newest_verdict_by_last_modified():
    # Codex round 3 (018): shadow_ids are random uuid hex — "largest id" chose an effectively
    # random verdict when several replays existed. Newest-by-LastModified is the contract.
    import datetime as _dt

    from app import quality as appq

    older = {"winner": "challenger", "name": "qa-demo", "modality": "text-generation",
             "challenger": {"version": "9"}, "shadow_id": "zzzzzz"}   # id sorts LAST
    newer = {"winner": "champion", "name": "qa-demo", "modality": "text-generation",
             "challenger": {"version": "9"}, "shadow_id": "aaaaaa"}   # id sorts FIRST
    fake = _ListingS3({
        f"{appq.SHADOW_PREFIX}zzzzzz.json": (older, _dt.datetime(2026, 7, 1)),
        f"{appq.SHADOW_PREFIX}aaaaaa.json": (newer, _dt.datetime(2026, 7, 2)),
    })
    best = _with_shadow_env(fake, None,
                            lambda: scheduler._default_shadow("qa-demo", "9", "llm"))
    assert best["winner"] == "champion" and best["shadow_id"] == "aaaaaa"  # newest, not max-id


def test_default_shadow_selection_is_total_when_a_verdict_lacks_last_modified():
    """019/US9 (FR-197): the prior newest-by-LastModified tie-break could never replace a chosen
    timestamped verdict with one whose LastModified was absent, and an all-missing-timestamp set was
    picked in listing order (effectively random). Selection is now total + deterministic: a verdict
    WITH a timestamp outranks one without (a real recency signal is never lost to a missing one), and
    the object key breaks any remaining tie — never order-dependent, never a raise on a mixed set."""
    import datetime as _dt

    from app import quality as appq

    stamped = {"winner": "champion", "name": "qa-demo", "modality": "text-generation",
               "challenger": {"version": "9"}, "shadow_id": "aaaaaa"}
    unstamped = {"winner": "challenger", "name": "qa-demo", "modality": "text-generation",
                 "challenger": {"version": "9"}, "shadow_id": "zzzzzz"}
    fake = _ListingS3({
        f"{appq.SHADOW_PREFIX}aaaaaa.json": (stamped, _dt.datetime(2026, 7, 1)),
        f"{appq.SHADOW_PREFIX}zzzzzz.json": (unstamped, None),   # no timestamp
    })
    best = _with_shadow_env(fake, None,
                            lambda: scheduler._default_shadow("qa-demo", "9", "llm"))
    assert best["shadow_id"] == "aaaaaa"    # the timestamped verdict outranks the un-timestamped one

    # all-missing timestamps → deterministic by object key (largest), not listing order
    a = {"winner": "champion", "name": "qa-demo", "modality": "text-generation",
         "challenger": {"version": "9"}, "shadow_id": "aaaaaa"}
    z = {"winner": "challenger", "name": "qa-demo", "modality": "text-generation",
         "challenger": {"version": "9"}, "shadow_id": "zzzzzz"}
    fake2 = _ListingS3({
        f"{appq.SHADOW_PREFIX}aaaaaa.json": (a, None),
        f"{appq.SHADOW_PREFIX}zzzzzz.json": (z, None),
    })
    best2 = _with_shadow_env(fake2, None,
                             lambda: scheduler._default_shadow("qa-demo", "9", "llm"))
    assert best2["shadow_id"] == "zzzzzz"   # deterministic: largest object key wins, not listing order


def test_default_shadow_listing_failure_raises_never_reads_as_green():
    # Codex round 4 / internal review (018): the whole-scan try/except returned None on ANY
    # failure, and None means "no shadow window exists" — GREEN. A transient store outage could
    # auto-promote a shadow-LOSING candidate. A listing failure must RAISE so the tick's
    # per-policy containment keeps the watch and retries next tick.
    class _BrokenS3:
        def list_objects_v2(self, **kw):
            raise RuntimeError("garage down")

    try:
        _with_shadow_env(_BrokenS3(), None,
                         lambda: scheduler._default_shadow("qa-demo", "9", "llm"))
    except Exception as e:
        assert "garage down" in str(e)
    else:
        raise AssertionError("a listing failure must raise, not read as no-shadow-window")


def test_default_shadow_skips_a_corrupt_key_without_vetoing_the_scan():
    # Codex round 4 (018): one unreadable/corrupt UNRELATED key used to abort the whole scan
    # (returning None → green). It must be skipped, keeping verdicts found around it.
    import datetime as _dt

    from app import quality as appq

    class _PoisonedS3:
        def __init__(self, objs, poisoned):
            self.objs, self.poisoned = objs, poisoned

        def list_objects_v2(self, Bucket, Prefix="", **kw):
            keys = sorted(set(self.objs) | {self.poisoned})
            newer = (None, _dt.datetime(2026, 7, 3))  # poisoned key lists NEWER than the good one
            return {"Contents": [{"Key": k, "LastModified": self.objs.get(k, newer)[1]}
                                 for k in keys if k.startswith(Prefix)]}

        def get_object(self, Bucket, Key):
            import io
            import json as _json

            if Key == self.poisoned:
                raise RuntimeError("corrupt object")
            return {"Body": io.BytesIO(_json.dumps(self.objs[Key][0]).encode())}

    verdict = {"winner": "champion", "name": "qa-demo", "modality": "text-generation",
               "challenger": {"version": "9"}, "shadow_id": "good01"}
    fake = _PoisonedS3(
        {f"{appq.SHADOW_PREFIX}good01.json": (verdict, _dt.datetime(2026, 7, 1))},
        poisoned=f"{appq.SHADOW_PREFIX}zz-corrupt.json")
    best = _with_shadow_env(fake, None,
                            lambda: scheduler._default_shadow("qa-demo", "9", "llm"))
    assert best is not None and best["shadow_id"] == "good01"  # found DESPITE the corrupt key


def test_default_shadow_ignores_windows_against_an_old_champion():
    # Codex round 6 (018): the serving alias can move between a replay and this evaluation — a
    # candidate that beat OLD v1 must never be green-lit over current v3 on that stale window.
    # Only verdicts whose recorded champion IS the incumbent count as shadow evidence.
    import datetime as _dt

    from app import quality as appq

    stale_win = {"winner": "challenger", "name": "qa-demo", "modality": "text-generation",
                 "challenger": {"version": "9"}, "champion": {"version": "1"},
                 "shadow_id": "stale1"}
    current_loss = {"winner": "champion", "name": "qa-demo", "modality": "text-generation",
                    "challenger": {"version": "9"}, "champion": {"version": "3"},
                    "shadow_id": "cur001"}
    fake = _ListingS3({
        f"{appq.SHADOW_PREFIX}stale1.json": (stale_win, _dt.datetime(2026, 7, 2)),   # NEWER
        f"{appq.SHADOW_PREFIX}cur001.json": (current_loss, _dt.datetime(2026, 7, 1)),
    })
    best = _with_shadow_env(fake, "3",
                            lambda: scheduler._default_shadow("qa-demo", "9", "llm"))
    assert best["shadow_id"] == "cur001"     # the stale v1 win is ignored despite being newest
    # with nothing serving there is no incumbent to compare — the newest verdict counts as before
    best = _with_shadow_env(fake, None,
                            lambda: scheduler._default_shadow("qa-demo", "9", "llm"))
    assert best["shadow_id"] == "stale1"


def test_failed_run_and_versionless_result_drop_the_watch():
    sched, promoted = _setup(mode="auto-on-green", run_status="failed")
    acts = sched.tick(now=100.0)
    assert any(a["action"] == "watched_run_failed" for a in acts)
    assert promoted == [] and policies.list_suggestions() == []

    sched, promoted = _setup(mode="auto-on-green", version=None)
    acts = sched.tick(now=100.0)
    assert any(a["action"] == "watched_run_no_version" for a in acts)
    assert promoted == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
