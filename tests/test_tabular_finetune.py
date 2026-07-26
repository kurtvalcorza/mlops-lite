"""025 US2 (T606) — tabular fine-tune flow, at the SEAMS (offline, no LightGBM/MLflow/Garage).

The full CPU run is live-gated (it needs LightGBM + MLflow + Garage, exactly like the other modality
flows' integration tests). What is pinned here is everything that can be checked without them:

  - `_parse_rows`: a DETERMINISTIC sorted feature union, missing features → 0.0, non-numeric/unlabeled
    rows skipped, and the two fail-fast cases (no usable rows / a single class — AUC undefined);
  - `flow_dispatch`: tabular is a valid modality and dispatch maps the request knobs onto the flow;
  - `_register`: writes the served child's exact `{"booster","features"}` bundle to the right key and
    registers with the `task=tabular` / `serving_engine=lightgbm` / CPU tags + lineage;
  - `_score_at_registration`: scores via the tabular prediction factory and, on failure, WARNS and
    returns None so a training success is never lost to a scoring failure.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "training")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flow_dispatch  # noqa: E402
from flows import tabular_finetune as tf  # noqa: E402


class StubBooster:
    """A booster stand-in (LightGBM absent offline): linear on the first feature."""

    def predict(self, X):
        return [float(row[0]) for row in X]


# --- _parse_rows -----------------------------------------------------------------------------------

def test_features_are_the_sorted_numeric_union_and_missing_default_to_zero():
    X, y, feats = tf._parse_rows([{"b": 1.0, "a": 2.0, "label": 1},
                                  {"c": 3.0, "label": 0}])
    assert feats == ["a", "b", "c"]                 # deterministic, sorted — the registered order
    assert X == [[2.0, 1.0, 0.0], [0.0, 0.0, 3.0]]  # missing → 0.0 (the served child's tolerance)
    assert y == [1, 0]


def test_label_is_excluded_and_non_numeric_values_ignored():
    X, y, feats = tf._parse_rows([{"x": 1.0, "note": "hi", "ok": True, "label": 1},
                                  {"x": 2.0, "label": 0}])
    assert feats == ["x"]                            # `note` (str) + `ok` (bool) + label all excluded
    assert X == [[1.0], [2.0]] and y == [1, 0]


def test_unlabeled_and_featureless_rows_are_skipped():
    X, y, _ = tf._parse_rows([{"x": 1.0, "label": 1},
                              {"x": 2.0},                    # no label → skipped
                              {"label": 0},                   # no features → skipped
                              {"x": 3.0, "label": "junk"},    # unparseable label → skipped
                              {"x": 4.0, "label": 0}])
    assert X == [[1.0], [4.0]] and y == [1, 0]


def test_no_usable_rows_fails_fast():
    for rows in ([], [{"nope": "x"}], [{"x": 1.0}]):
        try:
            tf._parse_rows(rows)
        except ValueError as e:
            assert "no usable rows" in str(e)
        else:
            raise AssertionError(f"expected ValueError for {rows!r}")


def test_single_class_fails_fast_because_auc_is_undefined():
    try:
        tf._parse_rows([{"x": 1.0, "label": 1}, {"x": 2.0, "label": 1}])
    except ValueError as e:
        assert "single class" in str(e)
    else:
        raise AssertionError("expected ValueError for a single-class dataset")


# --- dispatch --------------------------------------------------------------------------------------

def test_tabular_is_a_valid_dispatch_modality():
    assert "tabular" in flow_dispatch.VALID_MODALITIES


def test_dispatch_maps_the_request_knobs_onto_the_flow(monkeypatch):
    seen = {}

    def fake_flow(**kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setitem(sys.modules, "flows.tabular_finetune",
                        type(sys)("flows.tabular_finetune"))
    sys.modules["flows.tabular_finetune"].tabular_finetune_flow = fake_flow
    out = flow_dispatch.dispatch("tabular", {
        "dataset_name": "ds", "dataset_version": "v1", "output_name": "out",
        "num_leaves": 31, "learning_rate": 0.05, "n_estimators": 120, "seed": 7})
    assert out == {"ok": True}
    assert seen["dataset_name"] == "ds" and seen["output_name"] == "out"
    assert seen["num_leaves"] == 31 and seen["learning_rate"] == 0.05
    assert seen["n_estimators"] == 120 and seen["seed"] == 7


# --- _register (fake Garage + fake MLflow) ---------------------------------------------------------

class FakeS3:
    def __init__(self):
        self.puts = {}

    def put_object(self, Bucket, Key, Body):
        self.puts[(Bucket, Key)] = Body


class FakeMV:
    version = 4


class FakeClient:
    def __init__(self, *a, **kw):
        self.created = []
        self.versions = []

    def create_registered_model(self, name):
        self.created.append(name)

    def create_model_version(self, name, source, run_id, tags):
        self.versions.append({"name": name, "source": source, "run_id": run_id, "tags": tags})
        return FakeMV()


def test_register_writes_the_served_bundle_shape_and_tabular_tags(monkeypatch):
    s3, client = FakeS3(), FakeClient()
    dumped = {}

    def fake_dump(obj, buf):
        dumped.update(obj)
        buf.write(b"joblib-bytes")

    # Stub the exact submodules `_register` imports at call time. Patching through the `mlflow`
    # PACKAGE is fragile here — another suite replaces sys.modules['mlflow'] with a stub, so
    # `mlflow.tracking` may not be a bound attribute by the time this runs. `from X.Y import Z`
    # resolves sys.modules['X.Y'] first, so these entries are what the function actually sees.
    fake_tracking = type(sys)("mlflow.tracking")
    fake_tracking.MlflowClient = lambda *a, **kw: client
    fake_exceptions = type(sys)("mlflow.exceptions")
    fake_exceptions.MlflowException = type("MlflowException", (Exception,), {})
    monkeypatch.setitem(sys.modules, "mlflow.tracking", fake_tracking)
    monkeypatch.setitem(sys.modules, "mlflow.exceptions", fake_exceptions)
    monkeypatch.setitem(sys.modules, "joblib", type(sys)("joblib"))
    sys.modules["joblib"].dump = fake_dump
    monkeypatch.setattr(tf, "s3_client", lambda: s3)

    mv = tf._register("tab-out", StubBooster(), ["a", "b"], "run123", dataset_name="ds",
                      dataset_version="v1", base_model=None, parent_version=None,
                      parent_run_id=None)

    # the served child's exact load shape
    assert set(dumped) == {"booster", "features"} and dumped["features"] == ["a", "b"]
    # uploaded under <output>/<run>/model.joblib in the models bucket
    key = f"tab-out/run123/{tf.BUNDLE_NAME}"
    assert (tf.MODELS_BUCKET, key) in s3.puts
    assert mv == {"name": "tab-out", "version": "4", "source": f"s3://{tf.MODELS_BUCKET}/{key}"}
    tags = client.versions[0]["tags"]
    assert tags["task"] == "tabular" and tags["serving_engine"] == "lightgbm"
    assert tags["framework"] == "lightgbm" and tags["device"] == "cpu"


# --- _score_at_registration ------------------------------------------------------------------------

def test_score_at_registration_uses_the_factory_and_returns_the_result(monkeypatch):
    calls = {}

    def fake_score(name, version, modality, predict_fn, log_fn=None):
        calls.update(name=name, version=version, modality=modality)
        # the factory must be callable with the harness seam signature
        calls["scores"] = predict_fn([{"a": 2.0}], modality, version)
        return {"metric": "auc", "value": 0.91}

    import scoring
    monkeypatch.setattr(scoring, "score_at_registration", fake_score)
    out = tf._score_at_registration({"name": "m", "version": "4"}, StubBooster(), ["a"])
    assert out == {"metric": "auc", "value": 0.91}
    assert calls["modality"] == "tabular" and calls["version"] == "4"
    assert calls["scores"] == [2.0]          # numeric score from the factory, not a class


def test_score_failure_warns_and_leaves_the_version_registered(monkeypatch):
    import scoring

    def boom(*a, **kw):
        raise RuntimeError("benchmark exploded")

    monkeypatch.setattr(scoring, "score_at_registration", boom)
    logs = []
    monkeypatch.setattr(tf, "_log", lambda m, *a, **kw: logs.append(m))
    assert tf._score_at_registration({"name": "m", "version": "4"}, StubBooster(), ["a"]) is None
    assert any("WITHOUT an eval metric" in m for m in logs)   # warned, did not raise


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
