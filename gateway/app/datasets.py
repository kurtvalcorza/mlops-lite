"""Content-addressed dataset registry on Garage (T024/T025, US3).

DVC is the plan's default for data versioning, but it needs a git repo + the `dvc` CLI and a
git-commit per version — an awkward fit for a container-internal, API-driven flow, and it adds
weight that cuts against Principle III (Lightweight Footprint). Principle V (OSS & Swappable)
lets us deliver the *same guarantees* — named, versioned, **immutable** dataset references —
directly on the Garage `datasets` bucket via content addressing:

    a dataset version IS the sha256 of its bytes.

So re-registering identical content is idempotent (same version), and any change yields a new
immutable version. Swapping back to DVC later only touches this module + the router.

Layout:
    s3://datasets/<name>/<version>/data           # the bytes, immutable
    s3://datasets/<name>/<version>/manifest.json  # name, version, size, sha256, format, metadata
"""
import hashlib
import json
import time

from botocore.exceptions import BotoCoreError, ClientError

# 018 T362.1 (FR-176): the S3 client factory + bucket constants moved to platformlib so the training
# flows' reused cores (batch/quality/validation) import them from one shared place. Re-exported here
# so `datasets._s3` / `datasets.BUCKET` / `datasets.S3_ENDPOINT` stay valid for every existing caller.
from platformlib.s3io import BUCKET, S3_ENDPOINT, _s3  # noqa: F401


class DatasetError(Exception):
    """A dataset storage operation failed (object store unreachable or rejected the request)."""


def register_dataset(name: str, content: bytes, fmt=None, metadata=None) -> dict:
    """Store `content` as an immutable version of dataset `name`. Idempotent on identical bytes."""
    digest = hashlib.sha256(content).hexdigest()
    version = digest[:12]
    prefix = f"{name}/{version}"
    s3 = _s3()
    # Idempotent: identical content → identical version; return the existing manifest untouched.
    try:
        existing = s3.get_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json")
        m = json.loads(existing["Body"].read())
        m["already_existed"] = True
        return m
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
            raise DatasetError(str(e)) from e
    except BotoCoreError as e:
        raise DatasetError(str(e)) from e

    manifest = {
        "name": name,
        "version": version,
        "size_bytes": len(content),
        "sha256": digest,
        "format": fmt,
        "metadata": metadata or {},
        "uri": f"s3://{BUCKET}/{prefix}/data",
        "registered_at": time.time(),
    }
    try:
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}/data", Body=content)
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{prefix}/manifest.json",
            Body=json.dumps(manifest).encode(),
            ContentType="application/json",
        )
    except (ClientError, BotoCoreError) as e:
        raise DatasetError(str(e)) from e
    manifest["already_existed"] = False
    return manifest


def _prefixes(s3, prefix: str) -> list:
    """All CommonPrefixes under `prefix`, paginated (018 US1, FR-165): delimiter listings truncate
    at 1000 entries per page too, silently dropping datasets/versions past the first page. Inline
    (not `platformlib.store.list_common_prefixes`) because this module is still loaded trainer-side
    via the dual-runtime path hacks — the seams consolidate at T374."""
    out, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix, "Delimiter": "/"}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        out.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")
        if not token:
            return out


def _versions(s3, name: str) -> list:
    out = []
    for cp in _prefixes(s3, f"{name}/"):
        ver = cp[len(name) + 1:].rstrip("/")
        try:
            m = json.loads(s3.get_object(Bucket=BUCKET, Key=f"{name}/{ver}/manifest.json")["Body"].read())
            out.append({k: m.get(k) for k in ("version", "size_bytes", "sha256", "format", "uri")})
        except (ClientError, BotoCoreError):
            out.append({"version": ver})
    out.sort(key=lambda v: v.get("size_bytes") or 0)
    return out


def list_datasets() -> list:
    """Every registered dataset name with its immutable versions (paginated, FR-165)."""
    s3 = _s3()
    try:
        names = [p.rstrip("/") for p in _prefixes(s3, "")]
        return [{"name": n, "versions": _versions(s3, n)} for n in names]
    except (ClientError, BotoCoreError) as e:
        raise DatasetError(str(e)) from e


def get_dataset(name: str, version: str):
    """Resolve one dataset version → its manifest plus a presigned download URL (None if absent)."""
    s3 = _s3()
    try:
        m = json.loads(s3.get_object(Bucket=BUCKET, Key=f"{name}/{version}/manifest.json")["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise DatasetError(str(e)) from e
    except BotoCoreError as e:
        raise DatasetError(str(e)) from e
    try:
        m["download_url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": f"{name}/{version}/data"},
            ExpiresIn=3600,
        )
    except (ClientError, BotoCoreError):
        m["download_url"] = None
    return m
