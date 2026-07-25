"""025 US2 (T603/T605 — FR-351/FR-353) — the tabular child serves, and reports, a known version.

Prediction logging (`tests/test_tabular_quality.py`) attributes each served row to a model VERSION.
That attribution is only meaningful if the child actually serves the version the platform thinks it
does, so this pins the serving side of the contract, offline and CPU-only:

  - the child reports the version it ACTUALLY scored with (`model_version`), so the gateway can
    attribute rows to the booster that produced them rather than to a registry read (the
    agent-reported-identity rule 022 FR-260 established for the LLM path);
  - a WARM child reloads when the `@serving` alias moves. The pre-025 check re-resolved only on a
    cold load, but continuous traffic keeps refreshing `_last_used` so idle-release never fires — a
    promote left the old booster resident indefinitely. With logging in place that is worse than a
    stale model: rows produced by v1 would be logged under v2 and poison v2's quality window;
  - an unchanged version does NOT re-fetch the artifact, and a registry blip does NOT swap a
    working booster for the env fallback.

`joblib` is stubbed — it is a serving-child dependency, absent from the offline dev environment
(requirements-dev installs the gateway's set).
"""
import importlib.util
import io
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class _StubBooster:
    def predict(self, X):
        return [0.9] * len(X)


def _load_child(monkeypatch):
    fake_joblib = types.ModuleType("joblib")
    fake_joblib.load = lambda _fh: {"booster": _StubBooster(), "features": ["x1", "x2"]}
    monkeypatch.setitem(sys.modules, "joblib", fake_joblib)
    path = os.path.join(REPO, "serving", "children", "tabular_service.py")
    spec = importlib.util.spec_from_file_location("tabular_service_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeS3:
    def __init__(self):
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append((Bucket, Key))
        return {"Body": io.BytesIO(b"artifact-bytes")}


@pytest.fixture
def child(monkeypatch):
    mod = _load_child(monkeypatch)
    s3 = _FakeS3()
    monkeypatch.setattr(mod, "_s3", lambda: s3)
    mod._s3_calls = s3
    return mod


def _serve(child, rows=None):
    return child.predict(child.PredictRequest(rows=rows or [{"x1": 1.0, "x2": 2.0}]))


def test_child_reports_the_version_it_scored_with(child, monkeypatch):
    monkeypatch.setattr(child, "_resolve_target", lambda: ("models", "tab/v1/model.joblib", "1"))
    out = _serve(child)
    assert out["model_version"] == "1"
    assert out["predictions"] == [{"prediction": 1, "score": 0.9}]


def test_warm_child_reloads_when_the_serving_alias_moves(child, monkeypatch):
    """warm v1 → promote → v2 served, with no idle-release in between (the Codex round-5 gap)."""
    target = {"v": ("models", "tab/v1/model.joblib", "1")}
    monkeypatch.setattr(child, "_resolve_target", lambda: target["v"])
    assert _serve(child)["model_version"] == "1"
    assert len(child._s3_calls.gets) == 1

    # a promote moves the alias while the child stays warm (traffic keeps refreshing _last_used)
    target["v"] = ("models", "tab/v2/model.joblib", "2")
    child._resolved_at = 0.0                       # expire the resolve TTL, not the bundle
    out = _serve(child)
    assert out["model_version"] == "2"             # the NEW version is served...
    assert child._s3_calls.gets[-1] == ("models", "tab/v2/model.joblib")   # ...and was re-fetched


def test_same_version_does_not_refetch_the_artifact(child, monkeypatch):
    monkeypatch.setattr(child, "_resolve_target", lambda: ("models", "tab/v1/model.joblib", "1"))
    _serve(child)
    child._resolved_at = 0.0                       # TTL expires, but the version is unchanged
    _serve(child)
    assert len(child._s3_calls.gets) == 1          # no pointless reload of the same booster


def test_registry_blip_keeps_the_warm_booster(child, monkeypatch):
    """An unresolvable alias (version None) must NOT swap a working booster for the env fallback."""
    target = {"v": ("models", "tab/v2/model.joblib", "2")}
    monkeypatch.setattr(child, "_resolve_target", lambda: target["v"])
    assert _serve(child)["model_version"] == "2"

    target["v"] = (child.BUCKET, child.KEY, None)  # MLflow unreachable → fallback tuple
    child._resolved_at = 0.0
    out = _serve(child)
    assert out["model_version"] == "2"             # still the known-good resident version
    assert len(child._s3_calls.gets) == 1          # never re-fetched the fallback artifact


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
