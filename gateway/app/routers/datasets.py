"""Dataset registry router (T026, US3): register, list, and resolve immutable dataset versions.

Content arrives base64-encoded in the JSON body (handles binary; keeps the test stdlib-only).
For a local single-operator MVP that's adequate; multipart upload can be added later for large
files. Handlers are sync `def` — boto3 is blocking, so FastAPI runs them in its threadpool.
"""
import base64
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
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
    """Resolve one pinned dataset version → manifest + presigned download URL."""
    try:
        m = datasets.get_dataset(name, version)
    except datasets.DatasetError as e:
        raise HTTPException(status_code=502, detail=f"dataset store error: {e}")
    if m is None:
        raise HTTPException(status_code=404, detail=f"dataset '{name}' has no version {version}")
    return m
