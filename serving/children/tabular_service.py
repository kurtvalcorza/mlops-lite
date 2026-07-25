"""Slim tabular child (020 US2, T408) — FastAPI single-route replacement for the BentoML
tabular service (FR-203; contracts/children-api.md).

The LightGBM code moves verbatim from `serving/bento/tabular_service.py`; only the route
scaffolding changes (BentoML → one FastAPI `POST /predict` + `GET /readyz`). Same JSON request
(`{"rows": [...]}`), same `{model, device, features, predictions}` response values, same
dynamic-port launch contract (tabular_run.sh honors BENTO_HOST/BENTO_PORT).

CPU-only, off GPU admission, ALWAYS available — never touches VRAM or the agent's GPU slot.
Lazy-load + idle-release keep the scale-to-zero shape (only RAM here). The joblib artifact is a
dict: {"booster": lgb.Booster, "features": [...]} — the native Booster API keeps the dep light
(no scikit-learn).
"""
import io
import os
import threading
import time
from contextlib import asynccontextmanager

import boto3
import joblib
import numpy as np
from botocore.client import Config
from fastapi import FastAPI
from pydantic import BaseModel

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:3900")
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
BUCKET = os.getenv("MODELS_BUCKET", "models")
NAME = os.getenv("TABULAR_MODEL", "tabular-lgbm")
KEY = os.getenv("TABULAR_MODEL_KEY", f"{NAME}/v1/model.joblib")  # fallback if the registry is down
SERVING_ALIAS = os.getenv("TABULAR_ALIAS", "serving")
IDLE_TIMEOUT = float(os.getenv("TABULAR_IDLE_TIMEOUT", "600"))
# How long a resolved @serving target is trusted before the child re-checks the alias (025 US2). A
# warm child must notice a promotion without re-resolving on every single row, so the check is
# TTL-throttled rather than per-request.
RESOLVE_TTL = float(os.getenv("TABULAR_RESOLVE_TTL", "10"))


def _resolve_target():
    """The (bucket, key, version) of the @serving tabular version, resolved from MLflow: serve
    whatever version is currently promoted, not a fixed v1 key. `version` is the registry version
    string — the identity a served prediction is logged under (025 US2, FR-353) — or None when the
    registry is unreachable, in which case the env BUCKET/KEY fallback applies."""
    try:
        from mlflow.tracking import MlflowClient
        mv = MlflowClient(tracking_uri=MLFLOW_URI).get_model_version_by_alias(NAME, SERVING_ALIAS)
        src = mv.source or ""
        if src.startswith("s3://"):
            bucket, _, key = src[len("s3://"):].partition("/")
            if bucket and key:
                return bucket, key, str(mv.version)
    except Exception:
        pass
    return BUCKET, KEY, None


def _current_target():
    """`_resolve_target()` behind a short TTL so a warm child re-checks `@serving` cheaply."""
    global _resolved, _resolved_at
    now = time.time()
    if _resolved is None or (now - _resolved_at) > RESOLVE_TTL:
        _resolved, _resolved_at = _resolve_target(), now
    return _resolved


def _s3():
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],  # no hardcoded default (FR-017)
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"))


@asynccontextmanager
async def _lifespan(app):
    # Start the idle-release watcher at APP startup, not module import (review, PR#55): merely
    # importing this module from a test/tool must not spin a forever-thread that polls S3/MLflow.
    threading.Thread(target=_idle_watcher, daemon=True).start()
    yield


app = FastAPI(lifespan=_lifespan)

_bundle = None  # {"booster", "features"}
_loaded_version = None  # registry version of the RESIDENT booster (None = env-fallback artifact)
_resolved = None        # TTL-cached (bucket, key, version) from `_resolve_target()`
_resolved_at = 0.0
_last_used = 0.0
_lock = threading.Lock()


def _ensure_loaded():
    """Caller holds _lock. Lazy-load the joblib artifact on first use (scale-from-zero) AND reload
    it when the `@serving` alias has moved to a different version (025 US2, FR-351).

    The pre-025 check was `if _bundle is None`, which only re-resolved on a COLD load — but
    continuous traffic keeps refreshing `_last_used`, so idle-release never fires and a warm child
    kept serving the old booster indefinitely after a promotion moved the alias (Codex round-5).
    Reloading on a version change makes a promote take effect for every alias-moving caller (the
    operator promote, the scheduler's auto-on-green, suggestion acceptance) without any of them
    having to notify the child.

    A FAILED resolve (version None — registry blip) deliberately does NOT trigger a reload: a warm,
    working booster must not be swapped for the env fallback just because MLflow was briefly
    unreachable. CPU-only, no GPU lease."""
    global _bundle, _loaded_version, _last_used
    bucket, key, version = _current_target()
    if _bundle is None or (version is not None and version != _loaded_version):
        blob = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        _bundle = joblib.load(io.BytesIO(blob))
        _loaded_version = version
    _last_used = time.time()


def _idle_watcher():
    global _bundle, _loaded_version
    while True:
        time.sleep(30)
        with _lock:
            if _bundle is not None and (time.time() - _last_used) > IDLE_TIMEOUT:
                _bundle = None  # drop RAM; nothing to release (off-lease)
                _loaded_version = None  # no resident version to report until the next load



class PredictRequest(BaseModel):
    rows: list[dict]


@app.get("/readyz")
def readyz() -> dict:
    return {"ok": True}


@app.post("/predict")
def predict(req: PredictRequest) -> dict:
    """One prediction per input row from the single CPU joblib artifact (off-lease).

    Each row is a {feature: value} dict; missing features default to 0.0, extra keys are
    ignored, so the caller doesn't need to know the exact training column order."""
    global _last_used
    with _lock:
        _ensure_loaded()
        booster = _bundle["booster"]
        features = _bundle["features"]
        # Read the resident version INSIDE the lock, with the booster that produced these scores —
        # a concurrent promote+reload between scoring and responding must not mislabel the rows.
        version = _loaded_version
        X = np.array([[float(row.get(f, 0.0)) for f in features] for row in req.rows], dtype=float)
        scores = booster.predict(X)
        _last_used = time.time()
    preds = []
    for s in np.atleast_1d(scores):
        # Binary objective → a probability; threshold at 0.5 for the label, keep the score too.
        score = float(s)
        preds.append({"prediction": int(score >= 0.5), "score": round(score, 6)})
    # `model_version` (025 US2, FR-353): the registry version these rows were ACTUALLY scored by, so
    # the gateway can log version-scoped prediction rows a quality window can later score. None when
    # the registry was unreachable and the env-fallback artifact is resident — never a guess.
    return {"model": NAME, "model_version": version, "device": "cpu",
            "features": features, "predictions": preds}
