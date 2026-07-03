#!/usr/bin/env python3
"""Seed the ASR model into the registry (009 US3, T167 — FR-086).

whisper.cpp serves the ggml model locally (the registry entry is a routing pointer, exactly like the
serving LLM — the ASR supervisor never reads `source`). This registers + promotes a version tagged
`task=asr`, `serving_engine=whisper.cpp` so the gateway routes off registry metadata and the Infer tab
renders the transcribe panel from it.

Run in WSL with the training venv:  ~/mlops-train/bin/python scripts/seed_asr_model.py
"""
import os
import sys

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
NAME = os.getenv("ASR_MODEL", "whisper-base.en")
# Routing pointer only; whisper.cpp loads the ggml weights from the local host path (build.sh).
SOURCE = os.getenv("ASR_SOURCE", "s3://models/asr/whisper-base.en/ggml-base.en.bin")


def main() -> int:
    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.create_registered_model(NAME)
    except MlflowException:
        pass
    mv = c.create_model_version(
        name=NAME, source=SOURCE,
        tags={"kind": "asr", "format": "ggml", "task": "asr",
              "serving_engine": "whisper.cpp", "device": "cuda"})
    c.set_registered_model_alias(NAME, "serving", mv.version)
    print(f"registered {NAME} v{mv.version} -> {SOURCE}  (task=asr, serving_engine=whisper.cpp, "
          f"promoted @serving)")
    print("done. build ASR (the host agent then serves it at /engines/asr, 018 T359):  "
          "bash serving/whispercpp/build.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
