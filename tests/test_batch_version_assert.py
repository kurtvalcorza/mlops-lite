"""025 US1 (T596) — batch correctness, the offline-verifiable legs.

Two shipped fixes are covered here without a GPU or a live stack:
  1. the tabular batch payload — the flow must POST the child's `{"rows": [...]}` body, not the
     per-row `{"features": row}` that 422s against `serving/children/tabular_service.py` (FR-349);
  2. GPU-alias protection — an alias-named GPU batch (`text-generation`/`image-classification`) must
     trip `_gpu_batch_active` serving-holder protection, exactly like its canonical modality, or a
     promote reload / preempting request can change the engine mid-batch (FR-350).

The load-under-lease + batch-wide-exclusion + load-failure-restore + concurrent-inference legs
(FR-348/FR-350) drive a real GPU serving engine and the agent's request handling; they are the [HW]
task T599, validated on the box, not here.
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import batch_session as bs  # noqa: E402
from hostagent import jobs  # noqa: E402


def _load_batch_infer():
    # Load the native flow as a standalone module (Prefect absent → its decorator degrades to a no-op).
    path = os.path.join(REPO, "training", "flows", "batch_infer.py")
    spec = importlib.util.spec_from_file_location("batch_infer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- GPU-alias protection (hostagent/jobs.py) ------------------------------------------------------

def test_gpu_batch_alias_normalizes_to_protection():
    # canonical GPU modalities protect (unchanged)
    assert jobs._is_gpu_batch("llm") and jobs._is_gpu_batch("vision") and jobs._is_gpu_batch("asr")
    # the two admitted ALIASES must also protect — the gap this fixes
    assert jobs._is_gpu_batch("text-generation")       # -> llm
    assert jobs._is_gpu_batch("image-classification")  # -> vision
    # tabular is CPU/off-lease — never a GPU serving holder
    assert not jobs._is_gpu_batch("tabular")


# --- tabular batch payload (training/flows/batch_infer.py) -----------------------------------------

def test_tabular_predict_posts_rows_body_and_returns_prediction(monkeypatch):
    import httpx
    bi = _load_batch_infer()
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "t", "device": "cpu",
                    "predictions": [{"prediction": 1, "score": 0.87}]}

    def fake_post(url, json=None, timeout=None):
        captured["url"], captured["json"] = url, json
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    predict = bi._predict_fn("tabular")
    out = predict({"f1": 1.0, "f2": 2.0})

    # posts the child's batch body, NOT the {"features": row} that 422s
    assert captured["json"] == {"rows": [{"f1": 1.0, "f2": 2.0}]}
    assert captured["url"].endswith("/predict")
    # returns the single row's prediction dict (thresholded class + numeric score)
    assert out == {"prediction": 1, "score": 0.87}


# --- version-honoring ordering skeleton (hostagent/batch_session.py) ------------------------------
#
# Pure ordering with injected fakes (the real engine-load/exclusion/pointer seams are the [HW] T599):
# refuse-not-preempt, exclusion around the whole batch, load INSIDE the try, and a `finally` that
# re-reads the latest desired target (so a promote landing mid-batch is preserved, not clobbered).

class _Admission:
    def __init__(self, held=False):
        self._held = held

    def job_holds_gpu(self):
        return self._held


class _Exclusion:
    def __init__(self):
        self.events = []

    def acquire(self):
        self.events.append("acquire")
        return "batch-token-1"

    def release(self):
        self.events.append("release")


class _Engine:
    def __init__(self, fail=False):
        self.fail = fail
        self.loaded = []

    def load(self, target):
        self.loaded.append(target)
        if self.fail:
            raise RuntimeError(f"OOM loading {target}")


class _Desired:
    def __init__(self, value, fail_read=False, fail_restore=False):
        self.value = value        # tests mutate this to simulate a promote landing mid-batch
        self.restored = []
        self.fail_read, self.fail_restore = fail_read, fail_restore
        self._reads = 0

    def read(self):
        self._reads += 1
        # The FIRST read is the pre-acquire snapshot (nothing held yet); fail only the `finally`
        # re-read, which is the one that runs with the exclusion held.
        if self.fail_read and self._reads > 1:
            raise OSError("pointer store unreachable")
        return self.value

    def restore(self, target):
        if self.fail_restore:
            raise OSError("pointer store unreachable on write")
        self.restored.append(target)


def _session(adm, exc, eng, des):
    return bs.BatchSession(admission=adm, exclusion=exc, engine=eng, desired=des)


def test_session_happy_path_loads_before_scoring_then_restores():
    adm, exc, eng, des = _Admission(), _Exclusion(), _Engine(), _Desired("B")
    calls = []

    def score(token):
        calls.append((token, list(eng.loaded)))   # target must already be resident before scoring
        return "result"

    out = _session(adm, exc, eng, des).run("A", score)
    assert out == "result"
    assert eng.loaded == ["A"]                      # scored the REQUESTED version, not the resident B
    assert calls[0] == ("batch-token-1", ["A"])     # score got the bypass token; A loaded first
    assert des.restored == ["B"]                    # restored the desired target
    assert exc.events == ["acquire", "release"]     # exclusion spans the whole batch


def test_session_refuses_when_a_job_holds_the_gpu():
    adm, exc, eng, des = _Admission(held=True), _Exclusion(), _Engine(), _Desired("B")
    try:
        _session(adm, exc, eng, des).run("A", lambda token: "x")
    except bs.BatchRefused:
        pass
    else:
        raise AssertionError("expected BatchRefused when a job holds the GPU")
    assert eng.loaded == [] and exc.events == [] and des.restored == []   # never preempts / touches


def test_session_load_failure_still_restores_and_releases():
    adm, exc, eng, des = _Admission(), _Exclusion(), _Engine(fail=True), _Desired("B")
    scored = []
    try:
        _session(adm, exc, eng, des).run("A", lambda token: scored.append(token))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the load failure to propagate")
    assert scored == []                             # never scored — the load failed
    assert des.restored == ["B"]                    # prior/desired restored despite the load failure
    assert exc.events == ["acquire", "release"]     # exclusion still released


def test_session_restores_a_promotion_that_landed_mid_batch():
    adm, exc, eng, des = _Admission(), _Exclusion(), _Engine(), _Desired("B")

    def score(token):
        des.value = "C"                             # a promote lands mid-batch: desired B -> C
        return "result"

    _session(adm, exc, eng, des).run("A", score)
    assert des.restored == ["C"]                    # restored the NEWER desired, never the stale "B"


# --- a FAILING restore seam must not leak the exclusion, nor hide the batch's own cause ------------
#
# The restore path runs while the exclusion is HELD, so anything it raises used to skip
# `exclusion.release()` outright: online /infer would stay gated until the agent restarted (review
# round 7 flagged the narrower exception-masking half of this). Both halves are pinned below, because
# the real read/restore seams land later (T599, [HW]) behind exactly these interfaces.

def test_restore_read_failure_releases_the_exclusion_and_fails_loud():
    """A SUCCEEDED batch whose restore fails must report it — the engine may still hold the target."""
    adm, exc, eng, des = _Admission(), _Exclusion(), _Engine(), _Desired("B", fail_read=True)
    try:
        _session(adm, exc, eng, des).run("A", lambda token: "result")
    except OSError:
        pass
    else:
        raise AssertionError("a restore failure on a successful batch must propagate, not be silent")
    assert exc.events == ["acquire", "release"]      # THE LEAK: release must still have happened


def test_restore_write_failure_releases_the_exclusion_and_fails_loud():
    adm, exc, eng, des = _Admission(), _Exclusion(), _Engine(), _Desired("B", fail_restore=True)
    try:
        _session(adm, exc, eng, des).run("A", lambda token: "result")
    except OSError:
        pass
    else:
        raise AssertionError("a failed restore WRITE must propagate too")
    assert exc.events == ["acquire", "release"]


def test_restore_failure_never_displaces_the_batchs_own_exception():
    """When scoring already failed, the operator needs THAT cause — not the cleanup's."""
    adm, exc, eng, des = _Admission(), _Exclusion(), _Engine(), _Desired("B", fail_read=True)
    logs = []
    session = bs.BatchSession(admission=adm, exclusion=exc, engine=eng, desired=des,
                              log=lambda *a, **k: logs.append(" ".join(str(x) for x in a)))
    try:
        session.run("A", lambda token: (_ for _ in ()).throw(ValueError("scoring blew up")))
    except ValueError as e:
        assert "scoring blew up" in str(e)          # the BATCH's cause survives...
    except OSError:
        raise AssertionError("the cleanup failure displaced the batch's own exception")
    else:
        raise AssertionError("expected the scoring failure to propagate")
    assert any("RESTORE FAILED" in m for m in logs)  # ...and the cleanup failure is still reported
    assert exc.events == ["acquire", "release"]


def test_load_failure_with_a_failing_restore_still_releases():
    """Both seams down at once — the worst case still cannot leave online inference gated."""
    adm, exc, eng, des = _Admission(), _Exclusion(), _Engine(fail=True), _Desired("B", fail_read=True)
    try:
        _session(adm, exc, eng, des).run("A", lambda token: "unreached")
    except RuntimeError as e:
        assert "OOM" in str(e)                      # the load failure is still what surfaces
    except OSError:
        raise AssertionError("the cleanup failure displaced the load failure")
    assert exc.events == ["acquire", "release"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
