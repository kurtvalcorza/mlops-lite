"""Data-validation router (014 US2): validate a registered dataset version on demand.

`POST /datasets/{name}/{version}/validate` runs the hand-rolled readiness rules (validation.py) over the
dataset version's bytes and returns a structured ValidationReport — the **advisory** surface (the training
flow applies the same rules as a hard gate). Blocking (S3 + pure-Python rules) → sync work runs in the
FastAPI threadpool.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter
from pydantic import BaseModel

from .. import validation

router = APIRouter()

VALIDATIONS = Counter("gateway_validation_total", "Dataset validations", ["result"])


class ValidateRequest(BaseModel):
    """Optional per-call rule overrides — the defaults are sensible, not immutable (FR-135)."""
    format: Optional[str] = None
    required_columns: Optional[List] = None         # any-of groups, e.g. [["instruction","prompt"], ...]
    min_rows: Optional[int] = None
    max_null_rate: Optional[float] = None
    ranges: Optional[Dict[str, List[float]]] = None  # column -> [lo, hi]
    label_column: Optional[str] = None
    min_label_fraction: Optional[float] = None
    dispositions: Optional[Dict[str, str]] = None    # rule -> "gate" | "warn"


def _ruleset(req: "ValidateRequest") -> dict:
    """The non-None overrides as kwargs for validation.run_rules (ranges' [lo,hi] lists → tuples)."""
    ks = {}
    for k in ("required_columns", "min_rows", "max_null_rate", "label_column",
              "min_label_fraction", "dispositions"):
        v = getattr(req, k)
        if v is not None:
            ks[k] = v
    if req.ranges is not None:
        ks["ranges"] = {c: tuple(b) for c, b in req.ranges.items()}
    return ks


@router.post("/datasets/{name}/{version}/validate")
async def validate(name: str, version: str, req: Optional[ValidateRequest] = None):
    """Validate a dataset version → ValidationReport (passed + per-rule + stats). Advisory: this never
    blocks anything; the training flow enforces the same rules as a hard gate (FR-132)."""
    req = req or ValidateRequest()
    try:
        report = await run_in_threadpool(
            validation.validate_dataset, name, version, fmt=req.format, **_ruleset(req))
    except validation.ValidationError as e:
        VALIDATIONS.labels(result="error").inc()
        raise HTTPException(status_code=400, detail=str(e))
    VALIDATIONS.labels(result="passed" if report["passed"] else "failed").inc()
    return report
