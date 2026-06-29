"""Offline batch-inference core (014, US1) — score a whole dataset version, write results to MinIO.

The platform serves online/sync only; this adds the offline complement: iterate every row of a
registered dataset version through the **serving path** and write a **content-addressed** result
artifact back to MinIO (mirroring `datasets.py` — sha256 of the result bytes ⇒ immutable, idempotent,
downloadable, manifested).

**Mechanism (grilled A):** a GPU-backed batch runs as an **ephemeral Prefect flow on the native daemon**
(`training/flows/batch_infer.py`, launched via the gateway `POST /batch`) so it can hold the single GPU
lease **once** for the whole job (acquire → score all rows → release), exactly like an online tenant —
never pinning a second model in VRAM (Principle II). CPU/tabular models score **off-lease**. This module
is the **mechanism-clean scoring core**: `run_batch` (the per-row loop, record-and-continue, abort
threshold) and `write_result` (the content-addressed write) are pure of transport and take an injected
`predict_fn`, so they unit-test with no GPU and no live serving; `_live_predictor` supplies the real
serving call.
"""
import hashlib
import json
import os
import time

RESULTS_BUCKET = os.getenv("RESULTS_BUCKET", "results")
BATCH_PREFIX = "batch/"  # batch artifacts live beside drift/ + quality/ in the existing results bucket
# Don't abort on an early unlucky row — only enforce the failure-rate ceiling once enough rows have run.
ABORT_MIN_ROWS = int(os.getenv("BATCH_ABORT_MIN_ROWS", "5"))
DEFAULT_ABORT_THRESHOLD = float(os.getenv("BATCH_ABORT_THRESHOLD", "0.5"))  # abort if >X frac of rows fail


class BatchError(Exception):
    """A batch job failed fast (empty/unreadable dataset, unresolved model, or abort-threshold breach) —
    distinct from a per-row failure, which is recorded and the batch continues."""


def _s3():
    """The MinIO/S3 client, reused from the dataset registry. Lazy so the pure core imports without boto3."""
    try:
        from .datasets import _s3 as ds_s3
    except ImportError:
        from app.datasets import _s3 as ds_s3
    return ds_s3()


# --- the scoring loop (pure: injected predict_fn) -------------------------------------------------

def run_batch(rows: list, predict_fn, *, abort_threshold: float = DEFAULT_ABORT_THRESHOLD) -> dict:
    """Score every row via `predict_fn(row) -> output`, **recording per-row failures and continuing**
    (a bad row is written with its error, not fatal to the job). Aborts only if the running failure rate
    exceeds `abort_threshold` after at least `ABORT_MIN_ROWS` rows (FR-129). Returns the per-row results
    + counts; raises BatchError on an empty input or an abort."""
    if not rows:
        raise BatchError("empty dataset — nothing to score")
    results, failed = [], 0
    for i, row in enumerate(rows):
        try:
            results.append({"index": i, "input": row, "output": predict_fn(row), "ok": True})
        except Exception as e:  # record-and-continue — one bad row doesn't sink the job
            failed += 1
            results.append({"index": i, "input": row, "error": str(e), "ok": False})
            done = i + 1
            if done >= ABORT_MIN_ROWS and failed / done > abort_threshold:
                raise BatchError(
                    f"aborted at row {done}: failure rate {failed}/{done} exceeds {abort_threshold}")
    return {"results": results, "n_in": len(rows),
            "n_out": sum(1 for r in results if r["ok"]), "n_failed": failed}


# --- content-addressed result write (mirrors datasets.register_dataset) ----------------------------

def write_result(job_name: str, batch: dict, *, manifest_extra=None, s3=None) -> dict:
    """Write the batch results to MinIO **content-addressed** (sha256 of the result bytes ⇒ version), as
    `batch/<job>/<version>/{data,manifest.json}` — immutable + idempotent (identical results ⇒ same
    version). Returns the version + the result URI + the manifest."""
    s3 = s3 or _s3()
    # sort_keys ⇒ canonical bytes: identical *logical* results hash the same even if a row dict was
    # rebuilt with a different key insertion order (e.g. a replay deserialized from JSON).
    data = ("\n".join(json.dumps(r, sort_keys=True) for r in batch["results"]) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    version = digest[:12]
    base = f"{BATCH_PREFIX}{job_name}/{version}"
    manifest = {
        "job": job_name, "version": version, "sha256": digest,
        "n_in": batch["n_in"], "n_out": batch["n_out"], "n_failed": batch["n_failed"],
        "uri": f"s3://{RESULTS_BUCKET}/{base}/data", "created_at": time.time(),
        **(manifest_extra or {}),
    }
    try:
        s3.put_object(Bucket=RESULTS_BUCKET, Key=f"{base}/data", Body=data,
                      ContentType="application/x-ndjson")
        s3.put_object(Bucket=RESULTS_BUCKET, Key=f"{base}/manifest.json",
                      Body=json.dumps(manifest).encode(), ContentType="application/json")
    except Exception as e:
        raise BatchError(f"cannot write batch result: {e}") from e
    return {"version": version, "result_uri": manifest["uri"], "manifest": manifest}


# --- I/O wrapper: load a dataset version, score it, write the artifact -----------------------------

def _load_rows(dataset_name: str, dataset_version: str, s3=None) -> list:
    try:
        from .validation import parse_rows
    except ImportError:  # loaded standalone (tests) — resolve via the gateway dir on sys.path
        import sys
        gw = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if gw not in sys.path:
            sys.path.insert(0, gw)
        from app.validation import parse_rows
    s3 = s3 or _s3()
    bucket = os.getenv("DATASETS_BUCKET", "datasets")
    try:
        raw = s3.get_object(Bucket=bucket, Key=f"{dataset_name}/{dataset_version}/data")["Body"].read()
    except Exception as e:
        raise BatchError(f"cannot load dataset {dataset_name}@{dataset_version}: {e}") from e
    return parse_rows(raw)


def score_dataset(dataset_name: str, dataset_version: str, model_label: str, *, predict_fn=None,
                  abort_threshold: float = DEFAULT_ABORT_THRESHOLD, registry_version=None,
                  s3=None) -> dict:
    """Score a registered dataset version against `model_label` and write a content-addressed result.
    `predict_fn(row)->output` is the serving call (injected in tests; the live flow supplies the real
    per-modality predictor). Fails fast (BatchError) on an empty/unreadable dataset (no partial artifact)."""
    s3 = s3 or _s3()
    rows = _load_rows(dataset_name, dataset_version, s3=s3)
    if predict_fn is None:
        raise BatchError("no predict_fn — the native batch flow supplies the serving predictor")
    start = time.time()
    batch = run_batch(rows, predict_fn, abort_threshold=abort_threshold)
    job_name = f"{dataset_name}_{dataset_version}"
    written = write_result(job_name, batch, s3=s3, manifest_extra={
        "dataset": f"{dataset_name}@{dataset_version}", "model": model_label,
        "registry_version": registry_version, "elapsed_s": round(time.time() - start, 3)})
    return {
        "status": "succeeded", "dataset": f"{dataset_name}@{dataset_version}", "model": model_label,
        "n_in": batch["n_in"], "n_out": batch["n_out"], "n_failed": batch["n_failed"],
        "result_version": written["version"], "result_uri": written["result_uri"],
    }
