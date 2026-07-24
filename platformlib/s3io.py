"""Shared Garage/S3 client factory + bucket constants + paginated listings (018 T362.1, FR-176;
object-store home consolidated here 024 US1, T564).

Extracted from `gateway/app/datasets.py` so the gateway cores that the native training flows reuse
(`batch`, `quality`, `validation`) reach the object store through **one shared platformlib import**
instead of the fragile `from .datasets import _s3` dual-fallback each carried. `datasets.py`
re-exports these names, so its own routes and every external `datasets._s3` / `datasets.BUCKET`
reference are unchanged.

024 US1 folds the store's object-store access in HERE (rather than a second `objectstore.py` home):
the process-cached `s3_client()` and the paginated `list_keys` / `list_common_prefixes` moved from
`platformlib.store`, which re-exports them so `store.s3_client(...)` etc. are unchanged. boto3 is
imported **lazily** (inside the factory), so importing this module — and thus the `store` facade —
never needs the driver: the native daemons and the offline env load them boto3-free.

Credentials are read from the environment at build time — a missing var fails loudly by name, never
by value (FR-017).
"""
import os
import threading

#: Garage/S3 endpoint. `S3_ENDPOINT_URL` wins; else the MLflow artifact endpoint; else the compose host.
S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL") or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://garage:3900")
#: The dataset registry bucket (immutable, content-addressed dataset versions live here).
BUCKET = os.getenv("DATASETS_BUCKET", "datasets")

_client = None
_client_lock = threading.Lock()


def s3_client():
    """The process-cached Garage/S3 client (024 US1: moved here from `store.py`, re-exported).

    Distinct from `_s3()` (fresh per call) — this reuses one client for the store's listings/scans.
    Credentials required (FR-017 fail-loud). Reuses one client per process; boto3 clients are
    thread-safe for use (creation is not, hence the lock). boto3 is imported lazily so importing this
    module needs no driver."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from botocore.client import Config

                _client = boto3.client(
                    "s3",
                    endpoint_url=os.getenv("S3_ENDPOINT_URL")
                    or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://garage:3900"),
                    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                    region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                    config=Config(signature_version="s3v4"),
                )
    return _client


def _s3():
    """A fresh signature-v4 boto3 S3 client for Garage (datasets/batch/quality/validation import this).
    Per-call, NOT cached — its build-on-every-call behavior is load-bearing (the missing-creds-raises
    contract). Lazy so importing this module needs no creds; credentials are read from the environment
    when a client is actually built (FR-017 fail-loud)."""
    import boto3
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )


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
