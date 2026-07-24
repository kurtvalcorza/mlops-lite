"""025 US2 (T601) — tabular scoring: the prediction factory + the EXISTING `auc` metric + the gate.

Web-free, CPU-only, no LightGBM required (a stub booster stands in for the trained model — the factory's
contract is "score rows with whatever booster you were handed"). Pins:

  - `training.scoring.tabular.make_predict_fn` returns the eval-harness seam shape
    `predict_fn(rows, modality, version) -> [float score, ...]`, ordering columns by the registered
    feature list, tolerating missing features + extra row keys (the fixture's `label`);
  - the committed fixture `benchmarks/tabular/auc_smoke.jsonl` loads through 011's `load_benchmark`
    (registered in DEFAULT_BENCHMARKS — otherwise score-at-registration raises "no default benchmark");
  - `METRICS["tabular"]` is the pure-Python `auc` (promoted from stub, NOT re-implemented) and it ranks
    the factory's NUMERIC scores — a well-ordered model scores ~1.0, an inverted one ~0.0, and
    thresholding to the 0/1 class first destroys the ranking AUC measures;
  - `score_and_log(..., log=False)` glues factory→metric→result for the tabular modality.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import evaluation as ev  # noqa: E402

from training.scoring import tabular as tab  # noqa: E402

FEATURES = ["x1", "x2", "x3", "x4"]


class StubBooster:
    """Stands in for a trained LightGBM booster: a fixed linear rule over the same feature order the
    real bundle registers (x1 up / x3 down is the fixture's planted signal)."""

    def __init__(self, weights=(1.0, 0.0, -1.0, 0.0), flip=False):
        self.weights, self.flip = weights, flip
        self.seen = []

    def predict(self, X):
        self.seen.append(X)
        out = [sum(w * v for w, v in zip(self.weights, row)) for row in X]
        return [-s for s in out] if self.flip else out


def _bench():
    return ev.load_benchmark("tabular")


# --- the prediction factory (the eval-harness seam) -----------------------------------------------

def test_factory_returns_one_float_per_row_in_feature_order():
    b = StubBooster()
    predict = tab.make_predict_fn(b, FEATURES)
    rows = [{"x1": 1.0, "x2": 9.0, "x3": 0.0, "x4": 9.0, "label": 1},
            {"x1": 0.0, "x2": 9.0, "x3": 1.0, "x4": 9.0, "label": 0}]
    out = predict(rows, "tabular", "1")
    assert out == [1.0, -1.0]                       # w·x with the registered column order
    assert b.seen[0] == [[1.0, 9.0, 0.0, 9.0], [0.0, 9.0, 1.0, 9.0]]
    assert all(isinstance(s, float) for s in out)   # plain floats, not numpy scalars


def test_factory_defaults_missing_features_and_ignores_extra_keys():
    predict = tab.make_predict_fn(StubBooster(weights=(1.0, 1.0, 1.0, 1.0)), FEATURES)
    # x3/x4 absent → 0.0; `label` + `junk` ignored (never fed to the booster).
    assert predict([{"x1": 2.0, "x2": 3.0, "label": 1, "junk": "z"}], "tabular", "1") == [5.0]


# --- the committed fixture + the existing metric ---------------------------------------------------

def test_fixture_is_registered_and_loadable():
    bench = _bench()
    assert bench.name == "tabular/auc_smoke.jsonl" and bench.digest
    assert len(bench.rows) >= 20
    assert all("label" in r and r["label"] in (0, 1) for r in bench.rows)
    assert all(f in bench.rows[0] for f in FEATURES)
    # registered as the tabular default — score-at-registration resolves it with no explicit override.
    assert ev.DEFAULT_BENCHMARKS["tabular"] == "tabular/auc_smoke.jsonl"


def test_tabular_metric_is_the_existing_pure_python_auc():
    m = ev.METRICS["tabular"]
    assert m.name == "auc" and m.direction == ev.HIGHER
    assert m.score is ev.auc          # the EXISTING metric, not a re-implementation


def test_auc_ranks_the_factory_scores_on_the_fixture():
    bench = _bench()
    labels = [r["label"] for r in bench.rows]
    good = tab.make_predict_fn(StubBooster(), FEATURES)(bench.rows, "tabular", "1")
    bad = tab.make_predict_fn(StubBooster(flip=True), FEATURES)(bench.rows, "tabular", "1")
    auc_good, auc_bad = ev.auc(good, labels), ev.auc(bad, labels)
    assert auc_good > 0.9, auc_good        # the planted signal is recovered
    assert auc_bad < 0.1, auc_bad          # inverted scores ⇒ mirrored AUC
    assert abs((auc_good + auc_bad) - 1.0) < 1e-9


def test_thresholded_class_loses_the_ranking_auc_measures():
    """Why the factory must return the numeric probability, not the 0/1 class (Codex round-2)."""
    bench = _bench()
    labels = [r["label"] for r in bench.rows]
    scores = tab.make_predict_fn(StubBooster(), FEATURES)(bench.rows, "tabular", "1")
    classes = [1 if s >= 0.5 else 0 for s in scores]     # what logging the class would store
    assert ev.auc(scores, labels) > ev.auc(classes, labels)


# --- the glue: score_and_log over the tabular modality --------------------------------------------

def test_score_and_log_produces_a_tabular_auc_result_without_mlflow():
    from training.scoring import score_and_log
    bench = _bench()
    res = score_and_log("tab-model", 3, "tabular",
                        tab.make_predict_fn(StubBooster(), FEATURES), log=False)
    assert res["modality"] == "tabular" and res["metric"] == "auc"
    assert res["direction"] == ev.HIGHER and res["n"] == len(bench.rows)
    assert res["benchmark"] == "tabular/auc_smoke.jsonl" and res["benchmark_hash"] == bench.digest
    assert res["value"] > 0.9 and res["version"] == "3"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
