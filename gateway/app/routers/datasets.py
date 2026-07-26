"""Dataset registry router (T026, US3): register, list, and resolve immutable dataset versions.

Content arrives base64-encoded in the JSON body (handles binary; keeps the test stdlib-only).
For a local single-operator MVP that's adequate; multipart upload can be added later for large
files. Handlers are sync `def` — boto3 is blocking, so FastAPI runs them in its threadpool.
"""
import base64
import re
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from prometheus_client import Counter
from pydantic import BaseModel

from .. import datasets

router = APIRouter()

DATASET_OPS = Counter("gateway_dataset_ops_total", "Dataset registry operations", ["op", "status"])


class DatasetRegister(BaseModel):
    name: str
    content_b64: str
    format: Optional[str] = None
    metadata: Optional[Dict[str, str]] = None


@router.post("/datasets", status_code=201)
def register(req: DatasetRegister):
    """Register dataset content as an immutable, content-addressed version (idempotent)."""
    try:
        content = base64.b64decode(req.content_b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64")
    if not content:
        raise HTTPException(status_code=400, detail="dataset content is empty")
    try:
        m = datasets.register_dataset(req.name, content, req.format, req.metadata)
    except datasets.DatasetError as e:
        DATASET_OPS.labels(op="register", status="error").inc()
        raise HTTPException(status_code=502, detail=f"dataset store error: {e}")
    DATASET_OPS.labels(op="register", status="ok").inc()
    return m


@router.get("/datasets")
def list_datasets():
    """All registered datasets and their versions."""
    try:
        return {"datasets": datasets.list_datasets()}
    except datasets.DatasetError as e:
        raise HTTPException(status_code=502, detail=f"dataset store error: {e}")


@router.get("/datasets/{name}")
def get_dataset(name: str):
    """All immutable versions of one dataset (for comparison)."""
    try:
        all_ds = {d["name"]: d for d in datasets.list_datasets()}
    except datasets.DatasetError as e:
        raise HTTPException(status_code=502, detail=f"dataset store error: {e}")
    if name not in all_ds:
        raise HTTPException(status_code=404, detail=f"no dataset named '{name}'")
    return all_ds[name]


@router.get("/datasets/{name}/{version}")
def get_dataset_version(name: str, version: str):
    """Resolve one pinned dataset version → its manifest.

    025 US3 (FR-355): no presigned `download_url` — see `datasets.get_dataset`. Bytes come from
    `GET /datasets/{name}/{version}/download`."""
    try:
        m = datasets.get_dataset(name, version)
    except datasets.DatasetError as e:
        raise HTTPException(status_code=502, detail=f"dataset store error: {e}")
    if m is None:
        raise HTTPException(status_code=404, detail=f"dataset '{name}' has no version {version}")
    return m


# The download's advertised identity follows the manifest's `format` (review round 8) — a CSV or
# parquet version must not be handed to the browser as `.jsonl` / `application/x-ndjson`, which
# misleads the operator and any tool that sniffs on extension or content-type.
#
# Resolved through a WHITELIST, never by interpolating the stored value: `format` is a free-text hint
# persisted verbatim at registration (`datasets.register_dataset` does not validate it), so a value
# containing a quote would otherwise inject extra `Content-Disposition` parameters. Unknown or unset
# formats degrade to a generic binary download rather than guessing.
_DOWNLOAD_TYPES = {
    "jsonl": ("jsonl", "application/x-ndjson"),
    "ndjson": ("jsonl", "application/x-ndjson"),
    "json": ("json", "application/json"),
    "csv": ("csv", "text/csv"),
    "tsv": ("tsv", "text/tab-separated-values"),
    "parquet": ("parquet", "application/vnd.apache.parquet"),
    "txt": ("txt", "text/plain"),
}
_DEFAULT_DOWNLOAD = ("bin", "application/octet-stream")


def _download_identity(fmt):
    """(extension, content_type) for a manifest `format` — whitelisted, never interpolated."""
    return _DOWNLOAD_TYPES.get(str(fmt or "").strip().lower().lstrip("."), _DEFAULT_DOWNLOAD)


def _safe_filename_stem(name: str, version: str) -> str:
    """A `Content-Disposition` filename stem that cannot escape its quoted parameter.

    `name` is operator-supplied and equally unvalidated at registration, so it was already being
    interpolated into this header before the format work — sanitizing here closes that too."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", f"{name}-{version}")[:120]
    # A stem of only separators/dots is not a usable filename (and `..` is actively bad) — a name
    # that sanitizes away to nothing must still produce something the browser can save.
    return stem if re.search(r"[A-Za-z0-9]", stem) else "dataset"


@router.get("/datasets/{name}/{version}/download")
def download_dataset_version(name: str, version: str):
    """Stream a pinned dataset version's bytes THROUGH the gateway (025 US3, FR-355 — closes 021 FR-215).

    A byte proxy, deliberately NOT a presigned redirect: the presigned URL this replaced was signed
    against the internal object-store endpoint (`garage:3900`), so a browser could not resolve it — and
    handing one out would leak a signed object-store capability. Here the gateway (which holds the
    credentials) relays the object in bounded chunks, so the operator console downloads via the
    key-injecting BFF and no credential or signed URL ever reaches the browser."""
    try:
        manifest = datasets.get_dataset(name, version)     # the pinned version's declared format
        opened = datasets.open_dataset_bytes(name, version)
    except datasets.DatasetError as e:
        raise HTTPException(status_code=502, detail=f"dataset store error: {e}")
    if opened is None:
        raise HTTPException(status_code=404,
                            detail=f"dataset '{name}' has no data for version {version}")
    chunks, size = opened
    ext, content_type = _download_identity((manifest or {}).get("format"))
    headers = {"Content-Disposition":
               f'attachment; filename="{_safe_filename_stem(name, version)}.{ext}"'}
    if size is not None:
        headers["Content-Length"] = str(size)
    return StreamingResponse(chunks, media_type=content_type, headers=headers)
