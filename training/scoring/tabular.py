"""In-process tabular scorer (025 US2, T602) — probability scores from the in-memory LightGBM booster.

The trained booster **is** the served artifact (the tabular child loads the same
`{"booster", "features"}` joblib bundle — `serving/children/tabular_service.py`), so scoring runs the
still-resident trained model over the held-out benchmark in-memory: no quantization gap, no serving
round-trip, and CPU/off-lease (tabular holds no GPU lease — Principle II).

`make_predict_fn` returns a `predict_fn(rows, modality, version) -> [score, ...]` closure matching the
eval-harness seam, so a flow can call
`score_and_log(name, version, "tabular", make_predict_fn(booster, features))` right after registering.

**This module is a prediction factory, NOT a metric.** The primary metric already exists: 011's
pure-Python `auc` (`gateway/app/evaluation.py`) — a rank-sum ROC AUC. It is what `score_and_log` applies to
these scores, so AUC is never re-implemented here. The scores MUST be the **numeric probabilities**
(what AUC ranks), never the 0/1 thresholded class: thresholding first discards exactly the ranking
information AUC measures, collapsing a well-ordered model to a coin-flip-looking score.
"""


def make_predict_fn(booster, features):
    """Build a `predict_fn(rows, modality, version)` scoring each benchmark row with the in-memory
    `booster` over `features` (the same ordered feature list registered in the model bundle, so a row's
    columns line up exactly as the served child would order them).

    Missing features default to 0.0 and extra row keys (e.g. the benchmark's `label`) are ignored —
    matching the served child's tolerance, so the fixture rows need no special shaping. Returns one
    float probability per row, in row order, for 011's `auc` to rank.
    """
    def predict_fn(rows, _modality=None, _version=None):
        X = [[float(row.get(f, 0.0)) for f in features] for row in rows]
        scores = booster.predict(X)
        # LightGBM returns a numpy array for a batch and a scalar for a single row; normalize to a
        # plain list of floats so the pure-Python metric never depends on numpy semantics.
        try:
            return [float(s) for s in scores]
        except TypeError:  # a bare scalar
            return [float(scores)]

    return predict_fn
