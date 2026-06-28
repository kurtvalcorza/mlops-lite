#!/usr/bin/env python3
"""Seed a vision model into the platform (T022, US1 vision/audio half of FR-002).

Downloads a small pretrained image classifier (MobileNetV2), uploads its weights + ImageNet
labels to the MinIO `models` bucket, and registers an MLflow model version — so the BentoML
service packages the model *from the registry/object store* (depends on US2), not from torch hub.

Run in WSL with the training venv:  ~/mlops-train/bin/python scripts/seed_vision_model.py
"""
import io
import os
import sys

import boto3
import torch
from botocore.client import Config
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient
from torchvision.models import MobileNet_V2_Weights, mobilenet_v2

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000")
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
BUCKET = os.getenv("MODELS_BUCKET", "models")
NAME = os.getenv("VISION_MODEL", "vision-mobilenet")
KEY = f"{NAME}/v1/model.pt"


def _s3():
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],  # no hardcoded default (FR-017)
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"))


def main() -> int:
    print("downloading MobileNetV2 (ImageNet) ...")
    weights = MobileNet_V2_Weights.IMAGENET1K_V2
    model = mobilenet_v2(weights=weights)
    model.eval()

    buf = io.BytesIO()
    torch.save({"state_dict": model.state_dict(),
                "categories": list(weights.meta["categories"])}, buf)
    blob = buf.getvalue()

    print(f"uploading weights+labels to s3://{BUCKET}/{KEY} ({len(blob)//1024} KiB) ...")
    _s3().put_object(Bucket=BUCKET, Key=KEY, Body=blob)
    source = f"s3://{BUCKET}/{KEY}"

    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.create_registered_model(NAME)
    except MlflowException:
        pass
    mv = c.create_model_version(
        name=NAME, source=source,
        tags={"kind": "vision-classifier", "arch": "mobilenet_v2",
              "framework": "torchvision", "task": "image-classification", "device": "cpu"})
    print(f"registered {NAME} v{mv.version} -> {source}")
    print("done. start the bento:  bash serving/bento/run.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
