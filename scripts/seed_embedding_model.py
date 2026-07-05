#!/usr/bin/env python3
"""Seed an embedding model into the platform (009 US2, T162 — FR-086).

Downloads a small sentence-transformer (all-MiniLM-L6-v2), logs it via MLflow's
`sentence_transformers` flavor (so the BentoML embeddings service packages it from the registry —
FR-078), and registers + promotes the version with `task=embedding`, `serving_engine=bentoml` tags so
the gateway routes off registry metadata and the Infer tab renders the embed panel from it.

CPU-only / off-lease: embeddings *serving* never touches the GPU (only embeddings fine-tuning would).

Run in WSL with the training venv:  ~/mlops-train/bin/python scripts/seed_embedding_model.py
"""
import os
import sys

import mlflow
import mlflow.sentence_transformers as st_flavor
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient
from sentence_transformers import SentenceTransformer

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
NAME = os.getenv("EMBED_MODEL", "embed-minilm")
MODEL_ID = os.getenv("EMBED_MODEL_ID", "sentence-transformers/all-MiniLM-L6-v2")
EXPERIMENT = os.getenv("EMBED_EXPERIMENT", "mlops-lite-embeddings")


def main() -> int:
    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment(EXPERIMENT)

    print(f"downloading sentence-transformer {MODEL_ID} (CPU) ...")
    model = SentenceTransformer(MODEL_ID, device="cpu")

    print(f"logging via the MLflow sentence_transformers flavor + registering {NAME} ...")
    with mlflow.start_run(run_name=f"seed-{NAME}"):
        info = st_flavor.log_model(model, name="model", registered_model_name=NAME)
        print(f"  logged model at {info.model_uri}")

    # The registered version is the newest one for NAME; tag + promote it.
    c = MlflowClient(tracking_uri=MLFLOW_URI)
    versions = sorted(c.search_model_versions(f"name='{NAME}'"),
                      key=lambda mv: int(mv.version), reverse=True)
    if not versions:
        print(f"[FAIL] no registered version created for {NAME}", file=sys.stderr)
        return 1
    version = str(versions[0].version)
    for k, v in (("task", "embedding"), ("serving_engine", "bentoml"),
                 ("model_id", MODEL_ID), ("device", "cpu")):
        c.set_model_version_tag(NAME, version, k, v)
    try:
        c.set_registered_model_alias(NAME, "serving", version)
    except MlflowException as e:
        print(f"[FAIL] could not promote {NAME} v{version}: {e}", file=sys.stderr)
        return 1
    print(f"registered {NAME} v{version} (task=embedding, serving_engine=bentoml, promoted @serving)")
    print("done. start the service:  bash serving/children/embed_run.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
