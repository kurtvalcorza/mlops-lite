"""Shared MinIO/S3 client factory + bucket constants (018 T362.1, FR-176).

Extracted from `gateway/app/datasets.py` so the gateway cores that the native training flows reuse
(`batch`, `quality`, `validation`) reach the object store through **one shared platformlib import**
instead of the fragile `from .datasets import _s3` / `from app.datasets import _s3` dual-fallback each
carried (which only worked because a seam had injected `gateway/` onto `sys.path`). `datasets.py`
re-exports these names, so its own routes and every external `datasets._s3` / `datasets.BUCKET`
reference are unchanged.

boto3-only (no gateway imports), so it loads in the gateway image (which COPYs platformlib) and the
native training venv alike. Credentials are read from the environment at call time — a missing var
fails loudly by name, never by value (FR-017).
"""
import os

import boto3
from botocore.client import Config

#: MinIO/S3 endpoint. `S3_ENDPOINT_URL` wins; else the MLflow artifact endpoint; else the compose host.
S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL") or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000")
#: The dataset registry bucket (immutable, content-addressed dataset versions live here).
BUCKET = os.getenv("DATASETS_BUCKET", "datasets")


def _s3():
    """A signature-v4 boto3 S3 client for MinIO. Lazy-call so importing this module needs no creds;
    the credentials are read from the environment when a client is actually built."""
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        # No hardcoded default (FR-017): a missing var raises KeyError naming the var, never its value.
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )
