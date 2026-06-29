# Tabular: AutoGluon as an optional upgrade path (009 US4, FR-081)

**Status:** documentation only — **NOT** the default. The default tabular engine in mlops-lite is
**LightGBM** (a single joblib artifact, served on CPU off the GPU lease by
`serving/bento/tabular_service.py`). AutoGluon is recorded here as a drop-in upgrade for when you want
AutoGluon's automated tuning/stacking, **constrained so it stays a single-artifact, CPU, off-lease,
swap-in replacement** that honours the platform's Lightweight-Footprint principle (III).

This keeps Principle V (OSS & swappable): the serving interface is unchanged — a BentoML CPU service
exposing `predict(rows: list[dict])` behind the gateway's `POST /predict`. Only the artifact behind it
changes.

## Why it is not the default

AutoGluon pulls a heavy multi-model framework (and a strict version-pinning burden) into what is
otherwise a one-CPU-artifact path. For a single-operator machine that is disproportionate, so the
default stays LightGBM. Reach for AutoGluon only when its accuracy/automation genuinely earns the
weight.

## Constraints (so it stays a single-artifact, CPU, drop-in)

1. **Constrain hyperparameters to GBM.** Train AutoGluon's `TabularPredictor` with only the GBM
   (LightGBM) model family enabled, so the produced artifact is a gradient-boosted tree — not a large
   multi-model stack ensemble:

   ```python
   from autogluon.tabular import TabularPredictor

   predictor = TabularPredictor(label="target", problem_type="binary").fit(
       train_data,
       hyperparameters={"GBM": {}},   # GBM only — no NN/CAT/stacking blow-up
       num_stack_levels=0,            # no stacking
       num_bag_folds=0,               # no bagging ensemble
   )
   ```

2. **Deploy a single artifact via `clone_for_deployment(model='best')`.** Clone only the best model
   for deployment so the shipped artifact is one model, not the whole training zoo:

   ```python
   deploy_dir = predictor.clone_for_deployment(path="tabular-ag-deploy", model="best")
   # package `deploy_dir` as the single artifact the BentoML tabular service loads (CPU).
   ```

3. **Pin AutoGluon + Python versions.** AutoGluon artifacts are sensitive to the AutoGluon and Python
   versions used to fit them. Record both in the model version tags (and in
   `serving/bento/requirements.txt` if you adopt it), e.g.:

   ```
   autogluon.tabular==<pinned>   # pin exactly; AG artifacts are version-sensitive
   # python==3.x.y               # record the fit-time interpreter
   ```

   Tag the registered version with `serving_engine=bentoml`, `task=tabular`, plus
   `framework=autogluon`, `autogluon_version=<pinned>`, `python_version=<pinned>` so the swap is
   reproducible.

## Serving the swap

Point `serving/bento/tabular_service.py` at the AutoGluon artifact instead of the LightGBM joblib
(load the cloned predictor, call its `predict`), keeping the same `predict(rows: list[dict])` signature
and the same CPU / off-lease posture. The gateway, the BFF allowlist (`POST /predict`), and the Infer
tab's `predict` panel are unchanged — that is the point of routing off registry metadata (US1): a
modality's engine can change behind its `task` without re-plumbing the gateway or UI.
