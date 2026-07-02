"""Drift detection + report storage (T034, US5).

The plan names Evidently. Evidently pulls pandas + scipy + plotly, and that weight lands in the
*gateway image* — which lives on the constrained Windows C: drive (the real ~50 GB limit), against
Principle III (Lightweight Footprint). Principle V (OSS & Swappable) lets us compute the same
signal — distribution drift between a reference and a current dataset — with a dependency-free
**PSI** (Population Stability Index). Swapping Evidently back in only touches this module.

PSI per feature: bin the reference into deciles, compare the current distribution's bin shares.
  PSI < 0.1  no shift · 0.1–0.25 moderate · > 0.25 significant.
Dataset drift is declared when any numeric feature's PSI crosses the threshold (default 0.25).
Reports are written to the MinIO `results` bucket and the max score is exported as a metric.
"""
import csv
import io
import json
import math
import os
import time

from prometheus_client import Gauge

from .datasets import _s3  # reuse the MinIO/S3 client

DATASETS_BUCKET = os.getenv("DATASETS_BUCKET", "datasets")
RESULTS_BUCKET = os.getenv("RESULTS_BUCKET", "results")
DEFAULT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", "0.25"))

DRIFT_SCORE = Gauge("gateway_drift_score", "Max PSI across features from the last drift check")
DATASET_DRIFT = Gauge("gateway_dataset_drift", "1 if the last check exceeded the drift threshold")


class MonitorError(Exception):
    """A drift/monitoring operation failed (object store unreachable or bad data)."""


def _load_numeric(name: str, version: str) -> dict:
    """Pull a CSV dataset version from MinIO → {column: [floats]} (numeric columns only)."""
    try:
        raw = _s3().get_object(Bucket=DATASETS_BUCKET, Key=f"{name}/{version}/data")["Body"].read()
    except Exception as e:
        raise MonitorError(f"cannot load {name}@{version}: {e}") from e
    cols: dict = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
        for k, v in row.items():
            try:
                cols.setdefault(k, []).append(float(v))
            except (ValueError, TypeError):
                pass  # skip non-numeric cells
    out = {k: v for k, v in cols.items() if v}
    if not out:
        raise MonitorError(f"{name}@{version} has no numeric columns to compare")
    return out


def psi(ref: list, cur: list, bins: int = 10) -> float:
    """Population Stability Index for one feature, using reference deciles as bin edges."""
    ref_sorted = sorted(ref)
    if len(ref_sorted) < 2:
        return 0.0
    edges = sorted({ref_sorted[min(len(ref_sorted) - 1, int(round(q / bins * len(ref_sorted))))]
                    for q in range(1, bins)})

    def bucket(x):
        for i, e in enumerate(edges):
            if x <= e:
                return i
        return len(edges)

    nb = len(edges) + 1

    def dist(data):
        counts = [0] * nb
        for x in data:
            counts[bucket(x)] += 1
        tot = len(data) or 1
        return [c / tot for c in counts]

    rp, cp = dist(ref_sorted), dist(cur)
    eps = 1e-6
    return sum((c - r) * math.log((c + eps) / (r + eps)) for r, c in zip(rp, cp))


def compute_drift(ref_name: str, ref_ver: str, cur_name: str, cur_ver: str,
                  threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Compare two dataset versions feature-by-feature, store the report, export the score."""
    ref, cur = _load_numeric(ref_name, ref_ver), _load_numeric(cur_name, cur_ver)
    shared = sorted(set(ref) & set(cur))
    if not shared:
        raise MonitorError("reference and current datasets share no numeric columns")

    features = {c: round(psi(ref[c], cur[c]), 4) for c in shared}
    drifted = sorted(c for c, p in features.items() if p >= threshold)
    max_psi = max(features.values()) if features else 0.0
    report = {
        "reference": f"{ref_name}@{ref_ver}",
        "current": f"{cur_name}@{cur_ver}",
        "threshold": threshold,
        "features": features,
        "drifted_columns": drifted,
        "max_psi": round(max_psi, 4),
        "dataset_drift": bool(drifted),
        "created_at": time.time(),
    }
    DRIFT_SCORE.set(max_psi)
    DATASET_DRIFT.set(1 if drifted else 0)

    report_id = f"{cur_name}_{cur_ver}_{int(report['created_at'])}"
    try:
        _s3().put_object(
            Bucket=RESULTS_BUCKET, Key=f"drift/{report_id}.json",
            Body=json.dumps(report).encode(), ContentType="application/json",
        )
    except Exception as e:
        raise MonitorError(f"cannot store drift report: {e}") from e
    report["report_id"] = report_id
    return report


def latest_reports(limit: int = 20) -> list:
    """Recent drift reports from the results bucket, newest first.

    018 US1 (FR-165): paginated via `platformlib.store.list_keys` — a single `list_objects_v2`
    page silently truncated past 1000 reports, and report ids don't sort by time, so the "latest"
    view was an arbitrary slice once the prefix grew."""
    from platformlib import store

    s3 = _s3()
    try:
        keys = store.list_keys(s3, RESULTS_BUCKET, "drift/")
    except Exception as e:
        raise MonitorError(f"cannot list drift reports: {e}") from e
    reports = []
    for k in keys:
        try:
            reports.append(json.loads(s3.get_object(Bucket=RESULTS_BUCKET, Key=k)["Body"].read()))
        except Exception:
            pass
    reports.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return reports[:limit]
