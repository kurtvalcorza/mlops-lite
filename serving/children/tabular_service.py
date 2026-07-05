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


def _resolve_object():
    """The (bucket, key) of the @serving tabular version, resolved from MLflow: serve whatever
    version is currently promoted, not a fixed v1 key — so promoting a new artifact takes effect
    on the next (cold) load. Falls back to the env BUCKET/KEY if the registry is unreachable."""
    try:
        from mlflow.tracking import MlflowClient
        src = (MlflowClient(tracking_uri=MLFLOW_URI)
               .get_model_version_by_alias(NAME, SERVING_ALIAS).source or "")
        if src.startswith("s3://"):
            bucket, _, key = src[len("s3://"):].partition("/")
            if bucket and key:
                return bucket, key
    except Exception:
        pass
    return BUCKET, KEY


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
_last_used = 0.0
_lock = threading.Lock()


def _ensure_loaded():
    """Caller holds _lock. Lazy-load the joblib artifact on first use (scale-from-zero).
    Resolves the @serving version's object each cold load so a promotion is picked up after an
    idle-release/reload. CPU-only, no GPU lease."""
    global _bundle, _last_used
    if _bundle is None:
        bucket, key = _resolve_object()
        blob = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        _bundle = joblib.load(io.BytesIO(blob))
    _last_used = time.time()


def _idle_watcher():
    global _bundle
    while True:
        time.sleep(30)
        with _lock:
            if _bundle is not None and (time.time() - _last_used) > IDLE_TIMEOUT:
                _bundle = None  # drop RAM; nothing to release (off-lease)



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
        X = np.array([[float(row.get(f, 0.0)) for f in features] for row in req.rows], dtype=float)
        scores = booster.predict(X)
        _last_used = time.time()
    preds = []
    for s in np.atleast_1d(scores):
        # Binary objective → a probability; threshold at 0.5 for the label, keep the score too.
        score = float(s)
        preds.append({"prediction": int(score >= 0.5), "score": round(score, 6)})
    return {"model": NAME, "device": "cpu", "features": features, "predictions": preds}
