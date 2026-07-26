"""Tabular fine-tune flow (025 US2, T603 — FR-351/FR-354).

The last half-modality becomes whole. Tabular already SERVED (the LightGBM child loads a joblib bundle
`{"booster", "features"}` from the Garage `models` bucket — `serving/children/tabular_service.py`), but it
had no way to *produce* a version: no fine-tune flow, no committed eval fixture, a stub AUC. This flow
trains into that exact served shape, mirroring `vision_finetune.py`'s contract:

  fetch the dataset version → parse numeric feature rows + a binary `label` → train a LightGBM booster
  → write the `{"booster", "features"}` joblib bundle → upload to Garage → register an MLflow version
  tagged `task=tabular` / `serving_engine=lightgbm` / lineage → score-at-registration against the
  committed held-out AUC fixture (`benchmarks/tabular/auc_smoke.jsonl`) → log the metric on the version.

**CPU/off-lease (FR-354, Principle II).** Unlike every other fine-tune, tabular holds NO GPU lease: the
host agent admits it via `CPU_TRAINABLE_MODALITIES` and skips admission entirely (`hostagent/jobs.py`
`_needs_gpu`), so a tabular retrain can run while the GPU serves. There is no `free_cuda()` to call and
no VRAM to release. It is also deliberately NOT an HPO modality — `TRAINABLE_MODALITIES` (the `hpo`
kind's set) excludes tabular, since no tabular search space exists.

**No new dependency (FR-360).** LightGBM + joblib are already the tabular serving child's deps; they are
imported LAZILY inside `_train`/`_register` so this module imports (and the flow dispatches) without them
— exactly how the other flows keep torch out of the daemon process.

Dataset shape: JSONL rows of numeric features plus a binary label, e.g.
`{"x1": 0.7, "x2": 0.1, "label": 1}`. Feature columns are the sorted union of the numeric keys across
rows (minus the label), so the registered `features` list is deterministic and the served child orders
columns identically.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _common/lineage import as module or script
from _common import (  # noqa: E402
    MLFLOW_URI,
    MODELS_BUCKET,
    _log,
    fetch_jsonl,
    flow,
    s3_client,
)
from lineage import lineage_tags, link_parent_run, resolve_parent  # noqa: E402

TASK = "tabular"
LABEL_KEY = "label"
#: The served child reads this bundle name; keep the artifact filename in lockstep with it.
BUNDLE_NAME = "model.joblib"


def _parse_rows(rows: list):
    """Rows → (X, y, features). Features are the sorted union of NUMERIC keys except the label, so the
    column order is deterministic and reproducible; a row missing a feature contributes 0.0 (matching
    the served child's tolerance). Raises ValueError when the dataset has no usable labeled rows."""
    features = set()
    usable = []
    for r in rows:
        if not isinstance(r, dict) or r.get(LABEL_KEY) is None:
            continue
        try:
            label = int(r[LABEL_KEY])
        except (TypeError, ValueError):
            continue
        cols = {k: v for k, v in r.items() if k != LABEL_KEY and isinstance(v, (int, float))
                and not isinstance(v, bool)}
        if not cols:
            continue
        features.update(cols)
        usable.append((cols, label))
    if not usable:
        raise ValueError(f"tabular dataset has no usable rows — expected numeric features + a "
                         f"{LABEL_KEY!r} (0/1) per row")
    feats = sorted(features)
    X = [[float(cols.get(f, 0.0)) for f in feats] for cols, _ in usable]
    y = [label for _, label in usable]
    if len(set(y)) < 2:
        raise ValueError("tabular dataset has a single class — a binary label with both classes is "
                         "required to train (and for AUC to be defined)")
    return X, y, feats


def _train(X, y, features, *, num_leaves, learning_rate, n_estimators, seed):
    """Train the LightGBM binary classifier on CPU. Returns (booster, metrics). LightGBM is imported
    lazily (FR-360) — it is the tabular serving child's dep, not a new one."""
    import lightgbm as lgb

    train_set = lgb.Dataset(X, label=y, feature_name=list(features))
    params = {"objective": "binary", "num_leaves": int(num_leaves),
              "learning_rate": float(learning_rate), "seed": int(seed),
              "verbose": -1, "num_threads": int(os.getenv("TABULAR_TRAIN_THREADS", "2")),
              "deterministic": True, "force_row_wise": True}
    booster = lgb.train(params, train_set, num_boost_round=int(n_estimators))
    # Train AUC as a cheap recorded signal (the HELD-OUT metric is score-at-registration's job, below).
    scores = booster.predict(X)
    metrics = {"train_rows": len(X), "num_features": len(features),
               "n_estimators": int(n_estimators), "num_leaves": int(num_leaves),
               "positives": sum(1 for v in y if v == 1)}
    try:
        from platformlib.gateway_bridge import evaluation as _ev
        metrics["train_auc"] = round(float(_ev().auc([float(s) for s in scores], y)), 6)
    except Exception as e:  # noqa: BLE001 — a cheap extra signal must never fail the run
        _log(f"tabular: train-AUC signal unavailable ({e.__class__.__name__})")
    _log(f"tabular training done: rows={len(X)} features={len(features)} "
         f"train_auc={metrics.get('train_auc')}")
    return booster, metrics


def _register(output_name, booster, features, run_id, *, dataset_name, dataset_version,
              base_model, parent_version, parent_run_id):
    """Write the `{"booster", "features"}` joblib bundle (the served child's exact load shape), upload to
    Garage, and register an MLflow version tagged for tabular serving + lineage (FR-351)."""
    import joblib
    from mlflow.exceptions import MlflowException
    from mlflow.tracking import MlflowClient

    buf = io.BytesIO()
    joblib.dump({"booster": booster, "features": list(features)}, buf)
    key = f"{output_name}/{run_id}/{BUNDLE_NAME}"
    s3_client().put_object(Bucket=MODELS_BUCKET, Key=key, Body=buf.getvalue())
    source = f"s3://{MODELS_BUCKET}/{key}"

    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.create_registered_model(output_name)
    except MlflowException:
        pass
    tags = {"kind": "tabular-classifier", "framework": "lightgbm",
            "task": TASK, "serving_engine": "lightgbm", "device": "cpu",
            **lineage_tags(base_model, dataset_name, dataset_version, parent_version, parent_run_id)}
    mv = c.create_model_version(name=output_name, source=source, run_id=run_id, tags=tags)
    _log(f"registered {output_name} v{mv.version} <- run {run_id} ({len(features)} features, "
         f"task={TASK}, CPU/off-lease, NOT auto-promoted — promotion stays manual)")
    return {"name": output_name, "version": str(mv.version), "source": source}


def _score_at_registration(mv, booster, features):
    """025 US2 (FR-352): score the just-registered version against the committed held-out AUC fixture.
    The trained booster IS the served artifact, so it scores in-memory — no reload, no GPU, no serving
    round-trip. The prediction factory returns NUMERIC probabilities for 011's existing pure-Python
    `auc` to rank. Scoring failure warns and leaves the version registered WITHOUT a metric (the flow
    contract: a training success must not be lost to a scoring failure)."""
    training_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if training_root not in sys.path:
        sys.path.insert(0, training_root)
    from scoring import score_at_registration, tabular

    try:
        return score_at_registration(
            mv["name"], mv["version"], TASK,
            tabular.make_predict_fn(booster, features), log_fn=_log)
    except Exception as e:  # noqa: BLE001 — scoring is best-effort; the registered version stands
        _log(f"WARNING: score-at-registration setup failed for {mv['name']}@{mv['version']} "
             f"(tabular): {e} — version registered WITHOUT an eval metric")
        return None


@flow(name="tabular-finetune")
def tabular_finetune_flow(dataset_name: str, dataset_version: str, output_name: str,
                          base_model: str | None = None, num_leaves: int = 15,
                          learning_rate: float = 0.1, n_estimators: int = 60, seed: int = 0,
                          parent_version: str | None = None) -> dict:
    """End-to-end tabular fine-tune → registered, servable `task=tabular` version, CPU/off-lease.

    `parent_version` (optional) chains from a prior registered version of `output_name`; a parent whose
    `task` isn't tabular is rejected before training (`resolve_parent`). Hyperparameters are conservative
    defaults exposed on the Runs form, not hard-pinned.
    """
    import mlflow

    parent = resolve_parent(output_name, parent_version, TASK) if parent_version else None

    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("tabular-finetune")
    with mlflow.start_run() as run:
        run_id = run.info.run_id
        params = dict(modality="tabular", dataset_name=dataset_name,
                      dataset_version=dataset_version, output_name=output_name,
                      num_leaves=num_leaves, learning_rate=learning_rate,
                      n_estimators=n_estimators, seed=seed, parent_version=parent_version)
        mlflow.log_params(params)  # full config recorded → reproducible (FR-098/SC-062)
        link_parent_run(parent["run_id"] if parent else None)

        rows = fetch_jsonl(dataset_name, dataset_version)
        X, y, features = _parse_rows(rows)
        eval_result = None  # bound before scoring so the warn path still returns a shape
        booster, metrics = _train(X, y, features, num_leaves=num_leaves,
                                  learning_rate=learning_rate, n_estimators=n_estimators, seed=seed)
        mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
        mlflow.set_tag("device", "cpu")
        mv = _register(output_name, booster, features, run_id, dataset_name=dataset_name,
                       dataset_version=dataset_version, base_model=base_model,
                       parent_version=parent_version,
                       parent_run_id=parent["run_id"] if parent else None)
        eval_result = _score_at_registration(mv, booster, features)
        if eval_result:
            mlflow.set_tag("eval_metric", f"{eval_result['metric']}={eval_result['value']}")
        mlflow.set_tag("registered_version", mv["version"])
        return {"run_id": run_id, "model": mv, "metrics": metrics, "params": params,
                "eval": eval_result}


if __name__ == "__main__":
    import json
    a = sys.argv
    print(json.dumps(tabular_finetune_flow(
        dataset_name=a[1], dataset_version=a[2], output_name=a[3],
        n_estimators=int(a[4]) if len(a) > 4 else 60), indent=2))
