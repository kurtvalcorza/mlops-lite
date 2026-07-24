"""025 US1 (T596 / FR-348/350, closes SC-068) — batch inference asserts the requested version.

Offline coverage of the ordering/assertion logic in `training/flows/batch_infer.py`: a batch requesting
version A while B is resident asserts/loads A before scoring and NEVER scores B; it refuses cleanly if a
job holds the GPU (the reload is never a preemption). Pure — the transport (resident probe + reload) is
injected, so this runs with no GPU and no live serving. The real load-under-lease leg is hardware-gated
(T599/SC-175) and lives in the `[HW]` suite.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if os.path.join(REPO, "training") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "training"))

from flows import batch_infer as bi  # noqa: E402

# --- assert_version_resident: the pure ordering logic ---------------------------------------------

def test_no_pinned_version_skips_assertion():
    """No `registry_version` means "the current @serving model" — scoring the resident model is
    correct, so there is nothing to assert (and no probe is needed)."""
    calls = []
    out = bi.assert_version_resident("qwen", None,
                                     probe_fn=lambda: calls.append("probe") or ("qwen", "9"))
    assert out["asserted"] is False and "no pinned version" in out["reason"]
    assert calls == []  # never probed


def test_already_resident_asserts_without_reload():
    reloaded = []
    out = bi.assert_version_resident("qwen", "7", probe_fn=lambda: ("qwen", "7"),
                                     reload_fn=lambda *_: reloaded.append(True))
    assert out["asserted"] is True and out["reloaded"] is False
    assert reloaded == []  # already resident → no reload attempted


def test_mismatch_then_reload_makes_requested_version_resident():
    """Requested A while B is resident: the reload makes A live, the re-probe confirms A, scoring may
    proceed — and B is never the asserted version."""
    state = {"version": "B"}

    def probe():
        return ("qwen", state["version"])

    def reload_fn(model, version):
        state["version"] = str(version)  # the @serving target becomes resident

    out = bi.assert_version_resident("qwen", "A", probe_fn=probe, reload_fn=reload_fn)
    assert out["asserted"] is True and out["reloaded"] is True
    assert out["resident"] == ("qwen", "A")


def test_confirmed_mismatch_and_no_reload_refuses_never_scores_wrong_version():
    """A confirmed different resident version with no reload path → refuse (BatchVersionMismatch),
    never silently score the resident model."""
    with pytest.raises(bi.BatchVersionMismatch) as ei:
        bi.assert_version_resident("qwen", "A", probe_fn=lambda: ("qwen", "B"))
    assert "requested version A" in str(ei.value) and "holds B" in str(ei.value)


def test_reload_refusal_propagates_and_batch_refuses_cleanly():
    """If a job holds the GPU the reload raises (never preempts, FR-350) — the assertion surfaces that
    refusal rather than scoring the wrong version."""
    def reload_fn(model, version):
        raise bi.BatchVersionMismatch("reload refused (a job/batch holds the GPU — not preempted)")

    with pytest.raises(bi.BatchVersionMismatch) as ei:
        bi.assert_version_resident("qwen", "A", probe_fn=lambda: ("qwen", "B"), reload_fn=reload_fn)
    assert "not preempted" in str(ei.value)


def test_reload_that_still_leaves_wrong_version_refuses():
    """The reload can only make the promoted @serving target live (015 design); if that is still not
    the requested version, refuse rather than score the resident one."""
    def reload_fn(model, version):
        pass  # @serving is a different version → resident stays "B"

    with pytest.raises(bi.BatchVersionMismatch):
        bi.assert_version_resident("qwen", "A", probe_fn=lambda: ("qwen", "B"), reload_fn=reload_fn)


def test_unreported_version_is_tolerated_like_infer():
    """A legacy agent that doesn't report `registry_version` cannot be asserted against — tolerate with
    a note (matching the online /infer legacy tolerance) rather than refuse a working batch."""
    out = bi.assert_version_resident("qwen", "A", probe_fn=lambda: ("qwen", None))
    assert out["asserted"] is False and "unreported" in out["reason"]


# --- batch_infer_flow wiring: assertion runs before scoring, refusal blocks the score --------------

class _RecordingBatch:
    """A stand-in for the gateway batch core: records whether score_dataset was reached."""
    def __init__(self):
        self.scored = False

    def score_dataset(self, *a, **k):
        self.scored = True
        return {"status": "succeeded", "n_in": 0, "n_out": 0, "n_failed": 0,
                "result_version": "x", "result_uri": "s3://results/batch/x/data"}


def _patch_batch(monkeypatch):
    rec = _RecordingBatch()
    monkeypatch.setattr(bi, "_load_batch", lambda: rec)
    return rec


def test_flow_asserts_before_scoring(monkeypatch):
    rec = _patch_batch(monkeypatch)
    order = []

    def assert_fn(model, version):
        order.append(("assert", model, version))

    def predict_fn(row):
        return "out"

    bi.batch_infer_flow("ds", "v1", "qwen", modality="llm", registry_version="7",
                        predict_fn=predict_fn, assert_fn=assert_fn)
    assert order == [("assert", "qwen", "7")]  # assertion ran (with the requested version)
    assert rec.scored is True                  # and scoring followed


def test_flow_refusal_blocks_scoring(monkeypatch):
    rec = _patch_batch(monkeypatch)

    def assert_fn(model, version):
        raise bi.BatchVersionMismatch("wrong version resident")

    with pytest.raises(bi.BatchVersionMismatch):
        bi.batch_infer_flow("ds", "v1", "qwen", modality="llm", registry_version="7",
                            predict_fn=lambda r: "out", assert_fn=assert_fn)
    assert rec.scored is False  # never scored the wrong version


def test_flow_tabular_takes_no_lease_assertion(monkeypatch):
    """Tabular is CPU/off-lease: the default assertion is a no-op (no probe/reload), so an off-lease
    batch scores without a GPU-lease assertion."""
    rec = _patch_batch(monkeypatch)
    assert bi._default_assert_fn("tabular") is None
    bi.batch_infer_flow("ds", "v1", "tab-model", modality="tabular", registry_version="3",
                        predict_fn=lambda r: {"score": 1})
    assert rec.scored is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
