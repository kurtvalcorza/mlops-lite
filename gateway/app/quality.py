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
  - **US4 (T374): the high-churn INDEX moves to the relational `gateway` DB** (`platformlib.store`):
    a `predictions` row + a write-once `labels` row + a `capture_index` row per served prediction. The
    **bulky/variable bodies stay in MinIO** — the prediction OUTPUT at `payload_ref`, the recoverable
    input at `input_ref` — so scoring `window()` is one indexed predictions⋈labels join (FR-186,
    SC-111) instead of listing + reading every object. Quality REPORTS + shadow verdicts stay objects
    (no relational table). Write-once is now the `labels` PRIMARY KEY (FR-185) — the in-process
    `_label_write_lock` is gone.
  - **Fail posture (FR-164):** index-row WRITES fail-open with a dropped-counter (serving never
    blocks); window/label READS fail loud (`QualityStoreError` → 502).
  - **Dependency-light** — pure-Python + 011's metric libs; no Evidently/pandas/scipy (Principle III).
  - The scoring/breach/cooldown logic is split into **pure functions** (`score_window`,
    `evaluate_quality`, `is_breach`, cooldown helpers) so it unit-tests with no store and no GPU; the
    I/O wrappers (`log_prediction`, `attach_label`, `compute_quality`) layer storage on top.
"""
import json
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone

# prometheus_client, boto3 (via platformlib.s3io), and the evaluation module are imported lazily inside
# the functions that touch them, so the pure scoring/breach/cooldown logic imports — and unit-tests —
# with zero third-party deps (the same dependency-light stance as evaluation.py / monitoring.py).
from platformlib import store as _store_mod

RESULTS_BUCKET = os.getenv("RESULTS_BUCKET", "results")

# The relational store module (US4). A module-level indirection so tests inject an in-memory fake
# (`quality._store = FakeStore()`); production uses platformlib.store against the `gateway` DB.
_store = _store_mod
_conn_state = {"conn": None, "bootstrapped": False}
_conn_lock = threading.Lock()

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

# 016 shadow-replay input capture (FR-146/147). Storing the *recoverable* served input (prompt/image/audio)
# — not just a hash — lets a challenger be re-run over real traffic. It is SENSITIVE (full prompts/images/
# audio) and unbounded by nature, so it is gated by the existing QUALITY_CAPTURE_IO opt-in AND a bounded
# sampled+capped+TTL policy (Principle III — the constrained drive). Off ⇒ no replay corpus.
SHADOW_CAPTURE_SAMPLE = float(os.getenv("SHADOW_CAPTURE_SAMPLE", "1.0"))   # fraction of preds to capture
SHADOW_CAPTURE_CAP_N = int(os.getenv("SHADOW_CAPTURE_CAP_N", "500"))       # ring-buffer cap per modality
SHADOW_CAPTURE_TTL_S = float(os.getenv("SHADOW_CAPTURE_TTL_S", str(7 * 24 * 3600)))  # retention TTL (7d)

PRED_PREFIX, LABEL_PREFIX, QUALITY_PREFIX = "predictions/", "labels/", "quality/"
INPUT_PREFIX, SHADOW_PREFIX = "inputs/", "shadow/"  # 016: recoverable captured inputs + shadow verdicts

# Logging exports run on bounded daemon threads (the 006 pattern): drop a record rather than pile up
# threads when the store is slow, and never block process/test shutdown.
_MAX_CONCURRENT_LOGS = 4
_log_sem = threading.BoundedSemaphore(_MAX_CONCURRENT_LOGS)

_EVAL = None
_GAUGES = None


class QualityError(Exception):
    """A quality-monitoring operation failed (bad caller input, e.g. unknown modality / window_n<=0)."""


class QualityStoreError(QualityError):
    """A quality **store** I/O op failed (the results bucket is unreachable) — a subclass so the API can
    map an upstream outage to 502, distinct from a 400 bad-input QualityError."""


def _eval():
    """The 011 evaluation module (metric registry + direction), imported lazily so the pure logic
    stays dependency-free. US4/FR-176 retired the standalone `from app import evaluation` sys.path
    fallback — the trainer flows reach the cores through `platformlib` (T362.1), so quality.py is only
    ever loaded in-package (gateway) now."""
    global _EVAL
    if _EVAL is None:
        from . import evaluation
        _EVAL = evaluation
    return _EVAL


def _conn():
    """A cached store connection for the index rows, opened lazily and FAIL-OPEN — returns None when
    the store is unreachable (writes then drop with a counter; reads raise QualityStoreError). Bootstraps
    the schema once on first success (idempotent). `quality._store` is swappable for an in-memory fake."""
    with _conn_lock:
        c = _conn_state["conn"]
        if c is not None and not getattr(c, "closed", False):
            return c
        try:
            c = _store.connect()
            if not _conn_state["bootstrapped"]:
                _store.bootstrap(c)
                _conn_state["bootstrapped"] = True
            _conn_state["conn"] = c
            return c
        except Exception:  # noqa: BLE001 — fail-open: a store outage never blocks serving/logging
            _conn_state["conn"] = None
            return None


_DROPPED = None


def _dropped(kind: str) -> None:
    """Increment the fail-open drop counter (FR-164) when an index-row write is skipped/failed —
    observability for a store outage without ever blocking serving. Lazy so the module imports without
    prometheus_client."""
    global _DROPPED
    try:
        if _DROPPED is None:
            from prometheus_client import Counter
            _DROPPED = Counter("gateway_quality_store_dropped_total",
                               "Index-row writes dropped on a store outage (fail-open)", ["kind"])
        _DROPPED.labels(kind=kind).inc()
    except Exception:  # noqa: BLE001 — the counter itself must never break the fail-open path
        pass


def _invalidate_conn() -> None:
    """Drop (and close) the cached connection so the next `_conn()` reconnects. Called from the store-op
    error paths so a transient DB blip/restart SELF-HEALS: without this, a connection that broke but
    isn't flagged `closed` (e.g. a server-side idle-timeout drop) would make every subsequent read fail
    loud (502) forever until a manual `reset_store_conn()`. The bootstrap flag is kept (the schema is
    already applied — a reconnect need not re-run the DDL)."""
    with _conn_lock:
        c = _conn_state["conn"]
        _conn_state["conn"] = None
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001 — closing a broken connection may itself error; ignore
            pass


def reset_store_conn() -> None:
    """Test seam: drop the cached connection + bootstrap flag (so a test can swap `_store`)."""
    with _conn_lock:
        _conn_state["conn"] = None
        _conn_state["bootstrapped"] = False


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
    ts = time.time()
    captured_pred = prediction if QUALITY_CAPTURE_IO else None
    payload_ref = f"{PRED_PREFIX}{pid}.json"
    record = {
        "prediction_id": pid, "model_name": model_name, "model_version": str(model_version),
        "modality": modality, "ts": ts,
        "input_ref": input_ref if QUALITY_CAPTURE_IO else None,
        "prediction": captured_pred,
    }
    if not _log_sem.acquire(blocking=False):  # backpressure: store slow → drop this record
        _dropped("prediction")
        return pid

    def _run():
        # The OUTPUT body stays in MinIO (variable size); the INDEX row goes to the relational store so
        # the scoring window is an indexed join, not an object scan (US4). A `streamed` prediction has no
        # captured output → recorded with streamed=True, excluded from scoring by the caller.
        try:
            _put_json(payload_ref, record)
        except Exception:  # noqa: BLE001 — logging must never affect serving (fail-open)
            _dropped("prediction")
        try:
            conn = _conn()
            if conn is None:
                _dropped("prediction")
            else:
                _store.log_prediction(
                    conn, pid, model_name, str(model_version), modality,
                    datetime.fromtimestamp(ts, timezone.utc),
                    streamed=(captured_pred is None), payload_ref=payload_ref)
        except Exception:  # noqa: BLE001 — a store outage drops the index row, never serving
            _dropped("prediction")
            _invalidate_conn()  # so the next log reconnects instead of dropping on a stale connection
        finally:
            _log_sem.release()

    try:
        threading.Thread(target=_run, name="quality-log", daemon=True).start()
    except Exception:  # noqa: BLE001 — even a thread-spawn failure must not break serving
        _log_sem.release()
    return pid


def _s3():
    """The MinIO/S3 client. Lazy so the pure logic imports without boto3 (018 T362.1: from the shared
    platformlib factory, dropping the `.datasets`/`app.datasets` dual-fallback — FR-176)."""
    from platformlib.s3io import _s3 as ds_s3
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
    history. Returns a status dict (`attached` / `unknown` / `duplicate`).

    US4 (FR-185): write-once is now the `labels` PRIMARY KEY — a concurrent duplicate raises
    `store.LabelExists`, so the in-process `_label_write_lock` (a single-instance best-effort guard) is
    gone. Label writes are a READ-side operation (the operator expects a definite result), so a store
    outage FAILS LOUD (QualityStoreError → 502), unlike the fire-and-forget prediction/capture writes."""
    conn = _conn()
    if conn is None:
        raise QualityStoreError("store unreachable — cannot attach label")
    try:
        known = _store.prediction_exists(conn, prediction_id)
    except Exception as e:
        _invalidate_conn()  # a broken connection self-heals on the next request (not a permanent 502)
        raise QualityStoreError(f"cannot check prediction {prediction_id}: {e}") from e
    if not known:
        return {"prediction_id": prediction_id, "status": "unknown",
                "detail": "no logged prediction with this id"}
    try:
        _store.attach_label(conn, prediction_id, label, datetime.now(timezone.utc))
    except _store.LabelExists:
        return {"prediction_id": prediction_id, "status": "duplicate",
                "detail": "this prediction already has a label"}
    except Exception as e:
        _invalidate_conn()
        raise QualityStoreError(f"cannot store label for {prediction_id}: {e}") from e
    return {"prediction_id": prediction_id, "status": "attached"}


# --- 016: recoverable input capture for shadow-replay (pure policy + fire-and-forget storage) ------

# The replayable modalities (a single per-prediction (input, label) pair). Embeddings/tabular are out of
# scope (no clean per-request label) — see specs/016-shadow-replay/spec.md Non-Goals.
SHADOW_MODALITIES = {"text-generation", "image-classification", "asr"}
_MODALITY_ALIAS = {"llm": "text-generation", "vision": "image-classification"}


def normalize_modality(modality: str) -> str:
    """Map a friendly alias (llm/vision) to the registry `task` string; pass others through."""
    return _MODALITY_ALIAS.get(modality, modality)


def should_capture(*, sample: float = None, roll: float = None) -> bool:
    """Sampling admission for input capture (016 FR-147) — **pure**. Captures a `sample` fraction of
    predictions (1.0 = all, 0.0 = none). `roll` (a [0,1) draw) is injectable for deterministic tests;
    it defaults to `random.random()`. The master `QUALITY_CAPTURE_IO` opt-in is checked by the caller."""
    s = SHADOW_CAPTURE_SAMPLE if sample is None else sample
    if s >= 1.0:
        return True
    if s <= 0.0:
        return False
    return (random.random() if roll is None else roll) < s


def inputs_to_prune(records, *, now: float, cap_n: int = None, ttl_s: float = None) -> list:
    """Given `[(key, ts)]` captured-input records for ONE modality, return the keys to delete to enforce
    the **TTL** (older than `ttl_s`) and the **ring-buffer cap** (keep only the newest `cap_n`). Pure —
    unit-testable with synthetic records, no I/O. Bounds storage on the constrained drive (Principle III)."""
    cap_n = SHADOW_CAPTURE_CAP_N if cap_n is None else cap_n
    ttl_s = SHADOW_CAPTURE_TTL_S if ttl_s is None else ttl_s
    expired = {k for k, ts in records if ttl_s and ttl_s > 0 and (now - ts) > ttl_s}
    fresh = sorted(((k, ts) for k, ts in records if k not in expired), key=lambda kt: kt[1], reverse=True)
    over_cap = {k for k, _ in fresh[cap_n:]} if cap_n and cap_n > 0 else set()
    return sorted(expired | over_cap)


def _input_key(modality: str, prediction_id: str, ts: float) -> str:
    """Storage key for a captured input: `inputs/<modality>/<ts_ms padded>-<pid>.json`. The zero-padded
    millisecond timestamp in the key name makes a plain `list_objects` lexically time-sortable, so the
    prune + the replay-window selection need no per-record GET to find the newest N (cheap on the cap)."""
    return f"{INPUT_PREFIX}{normalize_modality(modality)}/{int(ts * 1000):015d}-{prediction_id}.json"


def parse_input_key(key: str):
    """Recover `(modality, ts, prediction_id)` from a captured-input key (the inverse of `_input_key`)."""
    rest = key[len(INPUT_PREFIX):]
    modality, _, fname = rest.partition("/")
    stamp, _, pid = fname[:-len(".json")].partition("-")
    return modality, int(stamp) / 1000.0, pid


def capture_input(prediction_id: str, modality: str, recoverable_input, *, options: dict = None,
                  roll: float = None) -> None:
    """016 (FR-146): store a **recoverable** served input (prompt/image-b64/audio-b64) under
    `inputs/<modality>/...` for a sampled prediction, so a challenger can be shadow-replayed over real
    traffic. `options` carries the served request settings whose scorer behaviour depends on them (LLM
    `max_tokens`/`temperature`, ASR `language`) so a replay reproduces the champion's decoding rather than
    the scorer defaults. Gated by `QUALITY_CAPTURE_IO` + the sampling policy; **fire-and-forget + fail-open**
    (never affects serving, like 013 logging) on the bounded `_log_sem`; lazy cap/TTL prune on write."""
    if (not QUALITY_LOGGING_ENABLED or not QUALITY_CAPTURE_IO
            or recoverable_input is None
            or normalize_modality(modality) not in SHADOW_MODALITIES
            or not should_capture(roll=roll)):
        return
    ts = time.time()
    mod = normalize_modality(modality)
    key = _input_key(modality, prediction_id, ts)
    record = {"prediction_id": prediction_id, "modality": mod,
              "input": recoverable_input, "ts": ts}
    if options:
        record["options"] = {k: v for k, v in options.items() if v is not None}
    if not _log_sem.acquire(blocking=False):  # backpressure: store slow → drop (serving unaffected)
        _dropped("capture")
        return

    def _run():
        # The recoverable input BODY stays in MinIO (`input_ref`); the `capture_index` row is the
        # indexed handle the shadow-replay window joins on (US4). Both fail-open.
        try:
            _put_json(key, record)
        except Exception:  # noqa: BLE001 — capture must never affect serving (fail-open)
            _dropped("capture")
        try:
            conn = _conn()
            if conn is None:
                _dropped("capture")
            else:
                _store.capture_input(conn, prediction_id, mod, key,
                                     datetime.fromtimestamp(ts, timezone.utc))
            _prune_inputs(mod)  # bound storage (cap + TTL) over the capture-index rows
        except Exception:  # noqa: BLE001 — capture must never affect serving (fail-open)
            _dropped("capture")
            _invalidate_conn()  # reconnect on the next capture rather than reuse a stale connection
        finally:
            _log_sem.release()

    try:
        threading.Thread(target=_run, name="shadow-capture", daemon=True).start()
    except Exception:  # noqa: BLE001 — even a thread-spawn failure must not break serving
        _log_sem.release()


def _prune_inputs(modality: str) -> None:
    """Enforce the per-modality cap + TTL by deleting old captures (best-effort). US4: the candidate set
    is the indexed `capture_index` rows (no object listing); each pruned entry drops BOTH the index row
    and its input-body object."""
    conn = _conn()
    if conn is None:
        return  # a store blip just defers the prune; the corpus is capped again on the next capture
    try:
        rows = _store.capture_rows(conn, modality)
    except Exception:  # noqa: BLE001 — prune is best-effort; never fail the capture
        _invalidate_conn()  # a broken connection reconnects on the next capture
        return
    by_ref = {r["input_ref"]: r["prediction_id"] for r in rows}
    records = [(r["input_ref"], r["captured_at"].timestamp()) for r in rows]
    s3 = _s3()
    for ref in inputs_to_prune(records, now=time.time()):
        pid = by_ref.get(ref)
        if pid:
            try:
                _store.delete_capture(conn, pid)
            except Exception:  # noqa: BLE001 — a prune miss is harmless
                pass
        try:
            s3.delete_object(Bucket=RESULTS_BUCKET, Key=ref)
        except Exception:  # noqa: BLE001 — a prune miss is harmless; never fail the capture
            pass


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

def _load_pairs(model_version: str, modality: str, model_name=None, *, window_n: int = None) -> list:
    """The labeled (prediction, label) window for the **same model + version + modality**, oldest→newest.

    US4 (FR-186, SC-111): this is now ONE indexed predictions⋈labels join (`store.window`) returning the
    newest `window_n` labeled rows — not a full object listing. The bulky OUTPUT body still lives at each
    row's `payload_ref`, so only those bounded ≤`window_n` objects are fetched (vs reading every logged
    prediction). MLflow versions are per-model, so the join filters model_name+version+modality (LLM v1 ≠
    vision v1). Rows with **no captured prediction** (streamed / capture-off → `payload_ref` output is
    None) are unscorable and excluded — never scored as "none"."""
    conn = _conn()
    if conn is None:
        raise QualityStoreError("store unreachable — cannot load the quality window")
    n = WINDOW_N if window_n is None else window_n
    try:
        rows = _store.window(conn, modality, model_name, str(model_version), n)  # DESC served_at LIMIT n
    except Exception as e:
        _invalidate_conn()  # reconnect on the next call rather than 502 every read after a blip
        raise QualityStoreError(f"cannot query the quality window: {e}") from e
    pairs = []
    for r in rows:
        ref = r.get("payload_ref")
        if not ref:
            continue
        try:
            pred = _get_json(ref)  # the OUTPUT body (bounded: only the ≤n windowed rows)
        except Exception:
            continue  # object pruned/missing → skip, never score a hole
        if pred.get("prediction") is None:
            continue  # streamed / capture-off → unscorable, excluded
        served = r.get("served_at")
        pairs.append({"prediction": pred.get("prediction"), "label": r.get("label"),
                      "ts": served.timestamp() if served is not None else 0})
    pairs.reverse()  # the query is newest-first; score_window slices the tail, so give it oldest→newest
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
    pairs = _load_pairs(model_version, modality, model_name=model_name, window_n=window_n)
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


def try_reserve_retrain(now: float = None, cooldown_sec: float = None):
    """**Atomically** check-and-start the cooldown: return a truthy TOKEN (the stamp — and begin
    the window) only if not already cooling down, else False. The quality trigger reserves before
    launching so two concurrent `/monitor/quality/check` calls can't both pass an in_cooldown()
    check and double-fire (Codex P2). Pass the token back to release_retrain() so a failed
    launch can only undo its OWN reservation."""
    cd = RETRAIN_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
    t = time.monotonic() if now is None else now
    global _last_retrain_monotonic
    with _cooldown_lock:
        if _last_retrain_monotonic > 0 and (t - _last_retrain_monotonic) < cd:
            return False
        _last_retrain_monotonic = t
        return t


def release_retrain(token: float = None) -> None:
    """Undo a reservation when the launch it guarded FAILED — so a trainer-down doesn't consume the
    cooldown (the next genuine breach can retry). Token-guarded (internal review, 018): with the
    token from try_reserve_retrain, the release is a no-op when a NEWER stamp landed in between
    (e.g. the policy scheduler's parked-retrain keep-alive refresh) — a failed launch must not
    clear someone else's live window. `None` keeps the unconditional pre-018 behavior for callers
    that hold no token (the scheduler's park paths, which own the stamp by construction)."""
    global _last_retrain_monotonic
    with _cooldown_lock:
        if token is None or _last_retrain_monotonic == token:
            _last_retrain_monotonic = 0.0


def reset_cooldown() -> None:
    """Clear the cooldown clock (test seam)."""
    global _last_retrain_monotonic
    with _cooldown_lock:
        _last_retrain_monotonic = 0.0
