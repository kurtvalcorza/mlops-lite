#!/usr/bin/env python3
"""Seed a tabular model into the platform (009 US4, T171 — FR-080/086).

Trains a small **LightGBM** binary classifier on synthetic numeric data (native Booster API — no
scikit-learn dep), dumps it as a single joblib artifact {"booster", "features"} to the Garage `models`
bucket, and registers + promotes the version with `task=tabular`, `serving_engine=bentoml` tags so the
gateway routes off registry metadata and the Infer tab renders the predict panel from it.

CPU-only / off-lease: tabular never touches the GPU.

Run in WSL with the training venv:  ~/mlops-train/bin/python scripts/seed_tabular_model.py
"""
import io
import os
import sys

import boto3
import joblib
import lightgbm as lgb
import numpy as np
from botocore.client import Config
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:3900")
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
BUCKET = os.getenv("MODELS_BUCKET", "models")
NAME = os.getenv("TABULAR_MODEL", "tabular-lgbm")
FEATURES = ["f0", "f1", "f2", "f3"]


def _s3():
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],  # no hardcoded default (FR-017)
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"))


def main() -> int:
    print("training a small LightGBM binary classifier on synthetic data (CPU) ...")
    rng = np.random.default_rng(0)
    n = 500
    X = rng.normal(size=(n, len(FEATURES)))
    # A simple separable-ish target: positive when the weighted sum crosses 0.
    y = (X @ np.array([1.5, -1.0, 0.5, 0.0]) + rng.normal(scale=0.5, size=n) > 0).astype(int)
    dtrain = lgb.Dataset(X, label=y, feature_name=FEATURES)
    booster = lgb.train(
        {"objective": "binary", "metric": "binary_logloss", "verbosity": -1,
         "num_leaves": 15, "learning_rate": 0.1},
        dtrain, num_boost_round=30)

    buf = io.BytesIO()
    joblib.dump({"booster": booster, "features": FEATURES}, buf)
    blob = buf.getvalue()

    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.create_registered_model(NAME)
    except MlflowException:
        pass
    # Write a VERSIONED object key so promoting a new version doesn't overwrite the prior artifact —
    # the service resolves the @serving version's source, so each version keeps its own bytes (Codex
    # review). The next version number is monotonic; the registered source is authoritative regardless.
    existing = c.search_model_versions(f"name='{NAME}'")
    next_v = max((int(mv.version) for mv in existing), default=0) + 1
    key = f"{NAME}/v{next_v}/model.joblib"
    source = f"s3://{BUCKET}/{key}"

    print(f"uploading joblib artifact to {source} ({len(blob)//1024} KiB) ...")
    _s3().put_object(Bucket=BUCKET, Key=key, Body=blob)

    mv = c.create_model_version(
        name=NAME, source=source,
        tags={"kind": "tabular", "arch": "lightgbm", "framework": "lightgbm",
              "task": "tabular", "serving_engine": "bentoml", "device": "cpu"})
    c.set_registered_model_alias(NAME, "serving", mv.version)
    print(f"registered {NAME} v{mv.version} -> {source}  (task=tabular, serving_engine=bentoml, "
          f"promoted @serving)")
    print("done. start the service:  bash serving/children/tabular_run.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
