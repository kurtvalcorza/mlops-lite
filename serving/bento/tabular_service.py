"""BentoML tabular service (009 US4, T170 — FR-080).

Serves a single **LightGBM** model (one joblib artifact) packaged from the MinIO `models` bucket
(seeded + registered by scripts/seed_tabular_model.py). Exposes `predict(rows: list[dict])`.

**CPU-only, off the GPU lease, ALWAYS available** (grilled 2026-06-28) — like embeddings and unlike
the vision service, it never imports gpu_lease and never touches VRAM, so a `predict` call succeeds
even while a GPU tenant holds the lease. Lazy-load + idle-release mirror the scale-to-zero shape of
the other services (only RAM here).

The joblib artifact is a dict: {"booster": lgb.Booster, "features": [...]} — the native Booster API
keeps the dep light (no scikit-learn). AutoGluon is documented as an optional, GBM-constrained,
single-artifact upgrade path (docs/tabular-autogluon-upgrade.md, FR-081) — NOT the default.

Serve natively in WSL:  bash serving/bento/tabular_run.sh   (gateway proxies POST /predict to it)
"""
import io
import os
import threading
import time

import bentoml
import boto3
import joblib
import numpy as np
from botocore.client import Config

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000")
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
BUCKET = os.getenv("MODELS_BUCKET", "models")
NAME = os.getenv("TABULAR_MODEL", "tabular-lgbm")
KEY = os.getenv("TABULAR_MODEL_KEY", f"{NAME}/v1/model.joblib")  # fallback if the registry is down
SERVING_ALIAS = os.getenv("TABULAR_ALIAS", "serving")
IDLE_TIMEOUT = float(os.getenv("TABULAR_IDLE_TIMEOUT", "600"))


def _resolve_object():
    """The (bucket, key) of the @serving tabular version, resolved from MLflow (Codex review): serve
    whatever version is currently promoted, not a fixed v1 key — so promoting a new artifact takes
    effect on the next (cold) load. Falls back to the env BUCKET/KEY if the registry is unreachable."""
    try:
        from mlflow.tracking import MlflowClient
        src = MlflowClient(tracking_uri=MLFLOW_URI).get_model_version_by_alias(NAME, SERVING_ALIAS).source or ""
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


@bentoml.service(name="tabular-service", traffic={"timeout": 60})
class TabularService:
    def __init__(self) -> None:
        self._bundle = None  # {"booster", "features"}
        self._last_used = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._idle_watcher, daemon=True).start()

    def _ensure_loaded(self):
        """Caller holds self._lock. Lazy-load the joblib artifact on first use (scale-from-zero).
        Resolves the @serving version's object each cold load so a promotion is picked up after an
        idle-release/reload. CPU-only, no GPU lease."""
        if self._bundle is None:
            bucket, key = _resolve_object()
            blob = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
            self._bundle = joblib.load(io.BytesIO(blob))
        self._last_used = time.time()

    def _idle_watcher(self):
        while True:
            time.sleep(30)
            with self._lock:
                if self._bundle is not None and (time.time() - self._last_used) > IDLE_TIMEOUT:
                    self._bundle = None  # drop RAM; nothing to release (off-lease)

    @bentoml.api
    def predict(self, rows: list[dict]) -> dict:
        """One prediction per input row from the single CPU joblib artifact (off-lease).

        Each row is a {feature: value} dict; missing features default to 0.0, extra keys are ignored,
        so the caller doesn't need to know the exact training column order.
        """
        with self._lock:
            self._ensure_loaded()
            booster = self._bundle["booster"]
            features = self._bundle["features"]
            X = np.array([[float(row.get(f, 0.0)) for f in features] for row in rows], dtype=float)
            scores = booster.predict(X)
            self._last_used = time.time()
        preds = []
        for s in np.atleast_1d(scores):
            # Binary objective → a probability; threshold at 0.5 for the label, keep the score too.
            score = float(s)
            preds.append({"prediction": int(score >= 0.5), "score": round(score, 6)})
        return {"model": NAME, "device": "cpu", "features": features, "predictions": preds}

    @bentoml.api
    def info(self) -> dict:
        return {"ok": True, "loaded": self._bundle is not None, "model": NAME,
                "device": "cpu", "task": "tabular", "lease": "off"}
