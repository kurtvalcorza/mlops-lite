"""Model-quality monitoring with ground truth (013) — the output-side complement to monitoring.py.

`monitoring.py` watches the **input** side (PSI over feature distributions). This module watches the
**output** side: it logs every served prediction with a stable id + the model version that produced it,
attaches (usually delayed) ground-truth labels by id, and computes **windowed per-modality quality**
over the labeled pairs — reusing **011's metric libraries** (accuracy / task-accuracy / WER / …). A
quality drop past a relative-to-baseline threshold flags a breach that can fire the **existing** retrain
trigger, combined with the input-PSI signal under an OR + cooldown policy.

Design (mirrors the siblings it lives beside):
  - **Prediction logging is the 006 tracing pattern** — fire-and-forget on a bounded daemon thread,
    lazy/best-effort, **fail-open**: a logging-store outage never alters or breaks the served response,
    and the write happens off the request path (Principle II / FR-119 / SC-075).
  - **Records ride the existing MinIO `results` bucket** (`predictions/`, `labels/`, `quality/` prefixes,
    mirroring `drift/`) — no new datastore (FR-121).
  - **Dependency-light** — pure-Python + 011's metric libs; no Evidently/pandas/scipy (Principle III).
  - The scoring/breach/cooldown logic is split into **pure functions** (`score_window`,
    `evaluate_quality`, `is_breach`, cooldown helpers) so it unit-tests with no S3 and no GPU; the I/O
    wrappers (`log_prediction`, `attach_label`, `compute_quality`) layer storage on top.
"""
import json
import os
import sys
import threading
import time
import uuid

# prometheus_client, boto3 (via .datasets), and the evaluation module are imported lazily inside the
# functions that touch them, so the pure scoring/breach/cooldown logic imports — and unit-tests — with
# zero third-party deps (the same dependency-light stance as evaluation.py / monitoring.py).

RESULTS_BUCKET = os.getenv("RESULTS_BUCKET", "results")

# --- configuration (operator-settable) ------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in _TRUTHY


QUALITY_LOGGING_ENABLED = _flag("QUALITY_LOGGING_ENABLED", True)   # log served predictions at all
QUALITY_CAPTURE_IO = _flag("QUALITY_CAPTURE_IO", True)             # store the input ref + prediction body
WINDOW_N = int(os.getenv("QUALITY_WINDOW_N", "100"))              # sliding window = last N labeled pairs
MIN_PAIRS = int(os.getenv("QUALITY_MIN_PAIRS", "20"))            # fewer labeled pairs ⇒ insufficient data
DROP_PCT = float(os.getenv("QUALITY_DROP_PCT", "0.10"))         # breach: >X% below the 011 baseline
RETRAIN_COOLDOWN_SEC = float(os.getenv("QUALITY_RETRAIN_COOLDOWN_SEC", "3600"))  # OR+cooldown debounce

PRED_PREFIX, LABEL_PREFIX, QUALITY_PREFIX = "predictions/", "labels/", "quality/"

# Logging exports run on bounded daemon threads (the 006 pattern): drop a record rather than pile up
# threads when the store is slow, and never block process/test shutdown.
_MAX_CONCURRENT_LOGS = 4
_log_sem = threading.BoundedSemaphore(_MAX_CONCURRENT_LOGS)

_EVAL = None
_GAUGES = None
# Serializes the label check-then-write so concurrent submissions for the same id can't both pass the
# duplicate check and the second overwrite the first (write-once, single-instance best-effort — S3 has no
# native compare-and-swap; a multi-replica gateway would need conditional puts, out of scope here).
_label_write_lock = threading.Lock()


class QualityError(Exception):
    """A quality-monitoring operation failed (bad caller input, e.g. unknown modality / window_n<=0)."""


class QualityStoreError(QualityError):
    """A quality **store** I/O op failed (the results bucket is unreachable) — a subclass so the API can
    map an upstream outage to 502, distinct from a 400 bad-input QualityError."""


def _eval():
    """The 011 evaluation module (metric registry + direction), imported lazily so the pure logic
    stays dependency-free. evaluation.py is stdlib-only, so this works in-package and standalone."""
    global _EVAL
    if _EVAL is None:
        try:
            from . import evaluation
        except ImportError:  # loaded standalone (tests) — resolve via the gateway dir on sys.path
            gw = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if gw not in sys.path:
                sys.path.insert(0, gw)
            from app import evaluation
        _EVAL = evaluation
    return _EVAL


def _gauges():
    """The output-side Prometheus gauges — the complement to monitoring.py's gateway_drift_score /
    gateway_dataset_drift. Created lazily so importing the module needs no prometheus_client."""
    global _GAUGES
    if _GAUGES is None:
        from prometheus_client import Gauge
        _GAUGES = {
            "score": Gauge("gateway_quality_score", "Windowed model-quality metric",
                           ["model_version", "modality"]),
            "breach": Gauge("gateway_quality_breach",
                            "1 if the last quality check breached the baseline",
                            ["model_version", "modality"]),
        }
    return _GAUGES


# --- US1: prediction logging (fire-and-forget, fail-open) -----------------------------------------

def log_prediction(model_name, model_version, modality, input_ref, prediction,
                   prediction_id=None) -> str:
    """Log a served prediction off the request path and return its **prediction id** (generated
    synchronously so the caller can return it regardless of whether the store write succeeds).

    Fire-and-forget + fail-open (FR-119): the MinIO write runs on a bounded daemon thread; any failure
    (or a disabled flag) is swallowed, so serving is never altered or broken. The input ref / prediction
    body are stored only when `QUALITY_CAPTURE_IO` is on (mirrors `MLFLOW_TRACE_CAPTURE_IO`)."""
    pid = prediction_id or uuid.uuid4().hex
    if not QUALITY_LOGGING_ENABLED:
        return pid
    record = {
        "prediction_id": pid, "model_name": model_name, "model_version": str(model_version),
        "modality": modality, "ts": time.time(),
        "input_ref": input_ref if QUALITY_CAPTURE_IO else None,
        "prediction": prediction if QUALITY_CAPTURE_IO else None,
    }
    if not _log_sem.acquire(blocking=False):  # backpressure: store slow → drop this record
        return pid

    def _run():
        try:
            _put_json(f"{PRED_PREFIX}{pid}.json", record)
        except Exception:  # noqa: BLE001 — logging must never affect serving (fail-open)
            pass
        finally:
            _log_sem.release()

    try:
        threading.Thread(target=_run, name="quality-log", daemon=True).start()
    except Exception:  # noqa: BLE001 — even a thread-spawn failure must not break serving
        _log_sem.release()
    return pid


def _s3():
    """The MinIO/S3 client, reused from the dataset registry (same as monitoring.py). Lazy so the pure
    logic imports without boto3."""
    try:
        from .datasets import _s3 as ds_s3
    except ImportError:
        from app.datasets import _s3 as ds_s3
    return ds_s3()


def _put_json(key: str, obj: dict) -> None:
    _s3().put_object(Bucket=RESULTS_BUCKET, Key=key,
                     Body=json.dumps(obj).encode(), ContentType="application/json")


def _get_json(key: str):
    return json.loads(_s3().get_object(Bucket=RESULTS_BUCKET, Key=key)["Body"].read())


def _list_keys(s3, prefix: str) -> list:
    """All object keys under `prefix`, paginating past the 1000-object `list_objects_v2` cap. Prediction
    ids are UUIDs (hex-sorted by S3, not by time), so a truncated first page would window over an
    arbitrary slice and silently drift the quality metric — the continuation loop avoids that."""
    keys, token = [], None
    while True:
        kw = {"Bucket": RESULTS_BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in page.get("Contents", []))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return keys


def _missing(e) -> bool:
    """True if an S3 head/get error means the key doesn't exist (a 404), vs a transient error — so a
    transient store blip isn't misreported as 'no such prediction' / 'no existing label'."""
    resp = getattr(e, "response", None)
    return isinstance(resp, dict) and resp.get("Error", {}).get("Code") in ("404", "NoSuchKey")


# --- US1: label ingestion (by prediction id, delayed-friendly) ------------------------------------

def attach_label(prediction_id: str, label) -> dict:
    """Attach a ground-truth label to a previously-logged prediction **by id** (FR-120). Supports
    delayed arrival; rejects an unknown id or a duplicate label cleanly — never overwriting served
    history. Returns a status dict (`attached` / `unknown` / `duplicate`)."""
    s3 = _s3()
    pred_key = f"{PRED_PREFIX}{prediction_id}.json"
    label_key = f"{LABEL_PREFIX}{prediction_id}.json"
    try:
        s3.head_object(Bucket=RESULTS_BUCKET, Key=pred_key)
    except Exception as e:
        if _missing(e):  # a real 404 → genuinely no such prediction
            return {"prediction_id": prediction_id, "status": "unknown",
                    "detail": "no logged prediction with this id"}
        raise QualityStoreError(f"cannot check prediction {prediction_id}: {e}") from e  # transient → surface
    # Serialize the duplicate-check + write so two concurrent submissions for the same id can't both
    # see "no label" and the second overwrite the first (write-once, Codex P2).
    with _label_write_lock:
        try:  # reject a duplicate — a label is write-once, served history can't be silently overwritten
            s3.head_object(Bucket=RESULTS_BUCKET, Key=label_key)
            return {"prediction_id": prediction_id, "status": "duplicate",
                    "detail": "this prediction already has a label"}
        except Exception as e:
            if not _missing(e):  # only a confirmed 404 means "no existing label" — never overwrite on a blip
                raise QualityStoreError(f"cannot check existing label for {prediction_id}: {e}") from e
        try:
            _put_json(label_key, {"prediction_id": prediction_id, "label": label, "ts": time.time()})
        except Exception as e:
            raise QualityStoreError(f"cannot store label for {prediction_id}: {e}") from e
    return {"prediction_id": prediction_id, "status": "attached"}


# --- US2: windowed quality (pure scoring split from I/O) -------------------------------------------

def _modality_metric(modality: str):
    """The 011 primary metric for a modality (accepting either a registry `task` string or a friendly
    alias), reusing 011's libs — no divergent definition (FR-122)."""
    task = _eval().METRICS  # task-keyed registry
    alias = {"llm": "text-generation", "vision": "image-classification"}
    key = modality if modality in task else alias.get(modality, modality)
    if key not in task:
        raise QualityError(f"no quality metric for modality {modality!r}")
    return task[key]


def score_window(pairs: list, modality: str, *, window_n: int = WINDOW_N, min_pairs: int = MIN_PAIRS):
    """Score the **last `window_n`** labeled (prediction, label) pairs for `modality` with 011's metric.

    `pairs` is the full chronologically-sorted list of labeled pairs; only the last `window_n` are
    scored (sliding count-based window). Returns `(metric_name, value, direction, n_used)`, or
    `(metric_name, None, direction, n_used)` when there are too few pairs to be meaningful
    (insufficient data — never a misleading number, FR-123)."""
    if window_n <= 0:  # `pairs[-0:]` is the WHOLE history, not an empty window — reject the invalid input
        raise QualityError(f"window_n must be positive, got {window_n}")
    metric = _modality_metric(modality)
    window = pairs[-window_n:]
    if len(window) < min_pairs:
        return metric.name, None, metric.direction, len(window)
    preds = [p["prediction"] for p in window]
    refs = [p["label"] for p in window]
    return metric.name, round(float(metric.score(preds, refs)), 6), metric.direction, len(window)


def is_breach(value, baseline, direction: str, drop_pct: float = DROP_PCT) -> bool:
    """True when `value` has degraded more than `drop_pct` (relative) from `baseline`, honouring the
    metric **direction** (FR-125): for higher-better metrics a *drop* below `baseline*(1-drop)` breaches;
    for lower-better metrics (WER/perplexity) a *rise* above `baseline*(1+drop)` breaches. No baseline ⇒
    nothing to compare against ⇒ no breach."""
    if value is None or baseline is None:
        return False
    margin = drop_pct * abs(baseline)
    if direction == _eval().LOWER:
        return value > baseline + margin
    return value < baseline - margin


def evaluate_quality(pairs: list, *, modality: str, model_version: str, baseline=None,
                     window_n: int = WINDOW_N, min_pairs: int = MIN_PAIRS,
                     drop_pct: float = DROP_PCT, model_name: str = None) -> dict:
    """Pure quality verdict over already-joined `pairs` (each `{prediction, label, ts}`): score the
    window, compare to the 011 `baseline`, and decide breach — with no I/O, so it unit-tests directly."""
    metric_name, value, direction, n_used = score_window(
        pairs, modality, window_n=window_n, min_pairs=min_pairs)
    insufficient = value is None
    breach = (not insufficient) and is_breach(value, baseline, direction, drop_pct)
    return {
        "model_name": model_name, "model_version": str(model_version), "modality": modality,
        "metric": metric_name, "value": value, "direction": direction,
        "n_labeled": len(pairs), "n_window": n_used, "window_n": window_n,
        "baseline": baseline, "drop_pct": drop_pct,
        "insufficient_data": insufficient, "breach": bool(breach),
        "created_at": time.time(),
    }


# --- US2: I/O wrapper — join from the store, compute, export, persist ------------------------------

def _load_pairs(model_version: str, modality: str, model_name=None) -> list:
    """Join logged predictions ↔ labels by id, chronologically (oldest→newest), for the **same model +
    version + modality** — MLflow versions are per-model, so LLM v1 and vision v1 coexist; filtering on
    version alone would score one model's rows against the other's metric (Codex P1). Only labeled
    predictions become pairs; unlabeled stay pending (excluded). Records with **no captured prediction**
    (streamed output, or `QUALITY_CAPTURE_IO=0`) are unscorable and excluded — never scored as "none"
    (Codex P1)."""
    s3 = _s3()
    try:
        keys = _list_keys(s3, PRED_PREFIX)  # paginated — never truncates past 1000 (review §1)
    except Exception as e:
        raise QualityStoreError(f"cannot list predictions: {e}") from e
    pairs = []
    for key in keys:
        try:
            pred = _get_json(key)
        except Exception:
            continue
        if str(pred.get("model_version")) != str(model_version):
            continue
        if pred.get("modality") != modality:
            continue  # like-for-like: don't mix modalities that share a version string
        if model_name and pred.get("model_name") != model_name:
            continue
        if pred.get("prediction") is None:
            continue  # uncaptured (streamed / capture-off) → unscorable, excluded (never scored wrong)
        pid = pred.get("prediction_id")
        try:
            label = _get_json(f"{LABEL_PREFIX}{pid}.json")
        except Exception:
            continue  # unlabeled → pending, excluded from scoring
        pairs.append({"prediction": pred.get("prediction"), "label": label.get("label"),
                      "ts": pred.get("ts", 0)})
    pairs.sort(key=lambda p: p["ts"])
    return pairs


def baseline_for(ev, metric_name):
    """The 011 baseline value only when it is the **same metric** as the one being scored (like-for-like)
    — else None (⇒ no breach). Without this guard a version whose 011 eval used a different metric (e.g.
    the lower-better perplexity fallback) would be compared against a higher-better accuracy window and
    fabricate a breach; 011's own gate refuses the same mismatch (compute_verdict)."""
    if not ev or ev.get("metric") != metric_name:
        return None
    return ev.get("value")


def _resolve_baseline(model_name, model_version, modality):
    """The 011 registered/eval baseline metric for this version (the relative-threshold reference), or
    None if the version is unevaluated or its eval used a different metric. Best-effort — never fails the
    quality check."""
    if not model_name:
        return None
    try:
        from . import registry
        ev = _eval().read_eval(registry._client(), model_name, model_version)
        return baseline_for(ev, _modality_metric(modality).name)  # like-for-like only
    except Exception:
        return None


def compute_quality(model_name, model_version, modality, *, baseline=None, window_n: int = WINDOW_N,
                    min_pairs: int = MIN_PAIRS, drop_pct: float = DROP_PCT, store: bool = True) -> dict:
    """Compute windowed quality for a model version from the store, export the gauges, and persist a
    QualityReport to the `results` bucket. The breach baseline defaults to the 011 eval baseline."""
    pairs = _load_pairs(model_version, modality, model_name=model_name)
    if baseline is None:
        baseline = _resolve_baseline(model_name, model_version, modality)
    report = evaluate_quality(pairs, modality=modality, model_version=model_version, baseline=baseline,
                              window_n=window_n, min_pairs=min_pairs, drop_pct=drop_pct,
                              model_name=model_name)
    g = _gauges()
    if report["value"] is not None:
        g["score"].labels(model_version=str(model_version), modality=modality).set(report["value"])
    # Always clear the breach gauge to the current verdict — including 0 on insufficient data — so a
    # stale breach=1 from a prior check can't keep showing a false active breach (Codex P2).
    g["breach"].labels(model_version=str(model_version), modality=modality).set(
        1 if report["breach"] else 0)
    if store:
        # ms precision + a short uuid suffix so two checks of the same version in the same second don't
        # collide and silently overwrite each other's report (review §5).
        report_id = f"{model_version}_{int(report['created_at'] * 1000)}_{uuid.uuid4().hex[:6]}"
        report["report_id"] = report_id
        try:
            _put_json(f"{QUALITY_PREFIX}{report_id}.json", report)
        except Exception as e:
            raise QualityStoreError(f"cannot store quality report: {e}") from e
    return report


def latest_quality_reports(limit: int = 20) -> list:
    """Recent quality reports from the results bucket, newest first (mirrors monitoring.latest_reports)."""
    s3 = _s3()
    try:
        keys = _list_keys(s3, QUALITY_PREFIX)  # paginated (review §1)
    except Exception as e:
        raise QualityStoreError(f"cannot list quality reports: {e}") from e
    reports = []
    for key in keys:
        try:
            reports.append(_get_json(key))
        except Exception:
            pass
    reports.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return reports[:limit]


# --- US3: combine policy — OR + cooldown (shared by the PSI + quality triggers) -------------------

_last_retrain_monotonic = 0.0
_cooldown_lock = threading.Lock()


def in_cooldown(now: float = None, cooldown_sec: float = None) -> bool:
    """True if a retrain fired within the cooldown window — the debounce that stops the two breach
    signals (input-PSI OR output-quality) from storming retrains (FR-126). Best-effort, in-process."""
    cd = RETRAIN_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
    now = time.monotonic() if now is None else now
    with _cooldown_lock:
        return _last_retrain_monotonic > 0 and (now - _last_retrain_monotonic) < cd


def note_retrain(now: float = None) -> None:
    """Record that a retrain just fired (starts the cooldown window)."""
    global _last_retrain_monotonic
    with _cooldown_lock:
        _last_retrain_monotonic = time.monotonic() if now is None else now


def try_reserve_retrain(now: float = None, cooldown_sec: float = None) -> bool:
    """**Atomically** check-and-start the cooldown: return True (and begin the window) only if not
    already cooling down, else False. The quality trigger reserves before launching so two concurrent
    `/monitor/quality/check` calls can't both pass an in_cooldown() check and double-fire (Codex P2)."""
    cd = RETRAIN_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
    t = time.monotonic() if now is None else now
    global _last_retrain_monotonic
    with _cooldown_lock:
        if _last_retrain_monotonic > 0 and (t - _last_retrain_monotonic) < cd:
            return False
        _last_retrain_monotonic = t
        return True


def release_retrain() -> None:
    """Undo a reservation when the launch it guarded FAILED — so a trainer-down doesn't consume the
    cooldown (the next genuine breach can retry). Symmetric with the PSI path, which only starts the
    cooldown on a successful launch. Safe because the window was free when we reserved."""
    global _last_retrain_monotonic
    with _cooldown_lock:
        _last_retrain_monotonic = 0.0


def reset_cooldown() -> None:
    """Clear the cooldown clock (test seam)."""
    global _last_retrain_monotonic
    with _cooldown_lock:
        _last_retrain_monotonic = 0.0
