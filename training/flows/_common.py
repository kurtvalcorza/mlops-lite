"""Shared trainer-flow infrastructure (010) — Garage client, config, and the ephemeral-Prefect shim.

The per-modality fine-tune flows (vision / embeddings / ASR) each own their *training logic* (so a
regression bisects to one modality — plan.md Structure Decision), but they share the same plumbing:
the Garage/MLflow endpoints, the boto3 client built from env-only credentials (no hardcoded default —
FR-017), and the optional-Prefect `flow`/`task`/`_log` shim the LLM flow established. Factoring that
here keeps the three new flows consistent without duplicating the boilerplate four ways.

The existing LLM flow (`finetune.py`) keeps its own copies verbatim — it is the hardware-validated path
and 010 deliberately does not re-plumb it (it only adopts the shared `lineage.py`).
"""
import os

# --- Config (env-overridable; defaults match finetune.py so a flow runs the same way) -------------
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:3900")
DATASETS_BUCKET = os.getenv("DATASETS_BUCKET", "datasets")
MODELS_BUCKET = os.getenv("MODELS_BUCKET", "models")


# --- Prefect (optional, ephemeral) ----------------------------------------------------------------
# Same posture as finetune.py: Prefect gives run structure + retries when present, and degrades to
# no-op decorators + plain prints when absent (Principle III — no always-on Prefect server).
try:
    from prefect import flow, task  # noqa: F401  (re-exported for the flows)
    from prefect.logging import get_run_logger

    def _log(msg):
        try:
            get_run_logger().info(msg)
        except Exception:
            print(msg, flush=True)
except Exception:  # Prefect absent → no-op decorators, plain prints
    def task(fn=None, **_):
        return fn if fn else (lambda f: f)

    def flow(fn=None, **_):
        return fn if fn else (lambda f: f)

    def _log(msg):
        print(msg, flush=True)


def s3_client():
    """boto3 S3 client for Garage. Credentials come from the environment only — no hardcoded default
    (FR-017); a missing key is a hard KeyError, not a silent anonymous client."""
    import boto3
    from botocore.client import Config
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )


def fetch_jsonl(name: str, version: str):
    """Pull a pinned dataset version from Garage and yield parsed JSON rows (one per non-blank line).

    Every modality stores its dataset as a single object at `{name}/{version}/data` (the shape the
    `/datasets` endpoint writes), so vision (`{image_b64,label}`), embeddings (`{anchor,positive[,
    negative]}`), and ASR (`{audio_b64,text}`) all parse the same way — only the row schema differs,
    which each flow validates itself.
    """
    import json

    raw = s3_client().get_object(Bucket=DATASETS_BUCKET, Key=f"{name}/{version}/data")["Body"].read()
    rows = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def free_cuda():
    """Drop the CUDA cache so nothing stays resident after a flow finishes (Principle II). Safe to
    call on CPU / when torch is absent."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
