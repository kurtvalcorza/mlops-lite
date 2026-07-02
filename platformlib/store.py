"""Shared storage helpers (018 T343/T349 — the object-store side of `platformlib.store`).

Promotes the S3 access patterns that grew as private functions of `gateway/app/quality.py` and
`gateway/app/datasets.py` into the one shared home (review §4.5): a module-level cached client
(instead of a fresh boto3 client per call) and **paginated** listings (instead of silent
truncation past 1000 objects). The relational side (`connect()`/`bootstrap()`, US4) lands in
T373 per contracts/store-schema.md.

boto3 is imported lazily so this module stays importable in stdlib-only contexts (the native
daemons import `platformlib.topology`/`contracts` without needing boto3 installed).
"""
import os
import threading

_client = None
_client_lock = threading.Lock()


def s3_client():
    """A cached S3 client (env-configured, credentials required — FR-017 fail-loud).

    Reuses one client per process; boto3 clients are thread-safe for use (creation is not,
    hence the lock)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from botocore.client import Config

                _client = boto3.client(
                    "s3",
                    endpoint_url=os.getenv("S3_ENDPOINT_URL")
                    or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000"),
                    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                    region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                    config=Config(signature_version="s3v4"),
                )
    return _client


def list_keys(s3, bucket: str, prefix: str) -> list:
    """All object keys under `prefix`, paginating past the 1000-object `list_objects_v2` page cap
    (FR-165). A truncated single page silently windows an arbitrary slice — never acceptable for
    operator-facing listings or monitoring windows."""
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")
        if not token:
            return keys


def list_common_prefixes(s3, bucket: str, prefix: str = "", delimiter: str = "/") -> list:
    """All CommonPrefixes under `prefix` (delimiter listings paginate too — the dataset registry's
    name/version listings truncated past 1000 entries before 018, FR-165)."""
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "Delimiter": delimiter}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        out.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")
        if not token:
            return out
