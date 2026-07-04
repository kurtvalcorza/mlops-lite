"""Offline harness for the 013 quality-monitoring + 018 US4 relational-store tests.

Loads `gateway/app/quality.py` as a package member (so its `from . import evaluation`/`registry`
relative imports resolve — 018 FR-176 retired the standalone fallbacks). Two in-memory fakes stand in
for the real backends so the join / label / capture / compute paths test without a live store:

  * **FakeS3** — the MinIO `results` bucket (the bulky prediction-OUTPUT and captured-INPUT bodies, plus
    the quality reports + shadow verdicts that stay objects under US4).
  * **FakeStore** — the relational `gateway` DB (`platformlib.store`): the `predictions` / `labels` /
    `capture_index` index rows the scoring `window()` and the shadow `replay_window()` join on. Mirrors
    the store *module surface* over dicts, enforcing the write-once `labels` PK (FR-185) and the
    (modality, model, version) window filters — so the cutover logic is exercised without Postgres.
"""
import os

from _pkgload import fresh_package, load_in_package
from platformlib import store as _platform_store  # for the real StoreError/LabelExists classes

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_quality():
    """A fresh `quality` module loaded package-relative (unique package per call, so the shadow tests'
    several configured instances don't share sibling state)."""
    return load_in_package(fresh_package(), "quality")


class FakeClientError(Exception):
    """Mimics botocore's ClientError shape so quality._missing() can tell a 404 from a transient error."""

    def __init__(self, code="404"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Minimal in-memory S3: enough surface for quality.py's put/get/head/list over the results bucket.
    Missing keys raise a 404-shaped ClientError (like real boto3), so the not-found vs transient
    distinction in the object reads is exercised faithfully."""

    def __init__(self):
        self.objs = {}  # key -> bytes

    def put_object(self, Bucket, Key, Body, **kw):
        self.objs[Key] = Body if isinstance(Body, bytes) else bytes(Body)

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise FakeClientError("NoSuchKey")
        import io
        return {"Body": io.BytesIO(self.objs[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objs:
            raise FakeClientError("404")
        return {}

    def delete_object(self, Bucket, Key):
        self.objs.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        # single un-truncated page (no IsTruncated) — quality._list_keys terminates after one call.
        return {"Contents": [{"Key": k} for k in sorted(self.objs) if k.startswith(Prefix)]}


class FakeStore:
    """In-memory stand-in for the `platformlib.store` MODULE surface (assigned to `quality._store`).

    Its methods take `conn` first (like the module functions) and ignore it — `connect()` returns the
    fake itself as the connection. It models exactly the contract the cutover relies on: write-once
    labels (a duplicate raises `LabelExists`), ON CONFLICT DO NOTHING for predictions/captures, and the
    (modality, model_name, version) + TTL filters of `window()` / `replay_window()`. A lock makes the
    write-once check atomic so the concurrency test is meaningful.
    """

    LabelExists = _platform_store.LabelExists
    StoreError = _platform_store.StoreError

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self.closed = False
        self.predictions = {}  # pid -> {model_name, version, modality, served_at, streamed, payload_ref}
        self.labels = {}       # pid -> {label, submitted_at}
        self.captures = {}     # pid -> {modality, input_ref, captured_at}

    # -- connection lifecycle (no-ops over the in-memory dicts) ------------------------------------
    def connect(self, dsn_str=None, *, autocommit=True):
        self.closed = False  # a fresh/reconnected connection (mirrors _invalidate_conn → reconnect)
        return self

    def close(self):
        self.closed = True

    def bootstrap(self, conn=None):
        return None

    # -- writes -------------------------------------------------------------------------------------
    def log_prediction(self, conn, prediction_id, model_name, version, modality, served_at,
                       *, streamed=False, payload_ref=None):
        with self._lock:
            self.predictions.setdefault(prediction_id, {  # ON CONFLICT DO NOTHING
                "model_name": model_name, "version": str(version), "modality": modality,
                "served_at": served_at, "streamed": streamed, "payload_ref": payload_ref})

    def attach_label(self, conn, prediction_id, label, submitted_at):
        with self._lock:
            if prediction_id in self.labels:  # write-once PK (FR-185)
                raise self.LabelExists(f"label already stored for {prediction_id}")
            self.labels[prediction_id] = {"label": label, "submitted_at": submitted_at}

    def capture_input(self, conn, prediction_id, modality, input_ref, captured_at):
        with self._lock:
            self.captures.setdefault(prediction_id, {  # ON CONFLICT DO NOTHING
                "modality": modality, "input_ref": input_ref, "captured_at": captured_at})

    # -- reads --------------------------------------------------------------------------------------
    def prediction_exists(self, conn, prediction_id):
        return prediction_id in self.predictions

    def window(self, conn, modality, model_name, version, n):
        rows = []
        for pid, p in self.predictions.items():
            # SQL parity: real `WHERE model_name = %s` never matches when the param is NULL (three-valued
            # logic), even against a NULL column — so a None arg matches NOTHING here too, not None==None.
            if (p["modality"] != modality or model_name is None or p["model_name"] != model_name
                    or p["version"] != str(version) or pid not in self.labels):
                continue
            lab = self.labels[pid]
            rows.append({"prediction_id": pid, "served_at": p["served_at"],
                         "payload_ref": p["payload_ref"], "label": lab["label"],
                         "submitted_at": lab["submitted_at"]})
        rows.sort(key=lambda r: r["served_at"], reverse=True)  # served_at DESC
        return rows[:n]

    def replay_window(self, conn, modality, model_name, version, n, *, ttl_cutoff=None):
        rows = []
        for pid, c in self.captures.items():
            if c["modality"] != modality:
                continue
            p, lab = self.predictions.get(pid), self.labels.get(pid)
            if not p or not lab:
                continue  # inner join: needs both a prediction row and a label
            # SQL parity: a NULL model_name param matches nothing (see window()).
            if model_name is None or p["model_name"] != model_name or p["version"] != str(version):
                continue
            if ttl_cutoff is not None and c["captured_at"] < ttl_cutoff:
                continue  # expired per the retention window (WHERE clause)
            rows.append({"prediction_id": pid, "input_ref": c["input_ref"],
                         "payload_ref": p["payload_ref"], "label": lab["label"],
                         "captured_at": c["captured_at"]})
        rows.sort(key=lambda r: r["captured_at"], reverse=True)  # captured_at DESC
        return rows[:n]

    def capture_rows(self, conn, modality):
        return [{"prediction_id": pid, "input_ref": c["input_ref"], "captured_at": c["captured_at"]}
                for pid, c in self.captures.items() if c["modality"] == modality]

    def delete_capture(self, conn, prediction_id):
        with self._lock:
            self.captures.pop(prediction_id, None)

    def has_captures(self, conn, modality):
        return any(c["modality"] == modality for c in self.captures.values())


class FakeGauge:
    """Records the last value set per label-set, so the gauge-export path is assertable offline."""

    def __init__(self):
        self.values = {}

    def labels(self, **kw):
        key = tuple(sorted(kw.items()))
        parent = self

        class _Setter:
            def set(self, v):
                parent.values[key] = v
        return _Setter()


def install_store(q, store=None):
    """Point the loaded module at a fresh in-memory FakeStore (the relational index) and drop any cached
    connection so `_conn()` reconnects to it. Returns the store for direct assertions."""
    fake = store or FakeStore()
    q._store = fake
    q.reset_store_conn()
    return fake


def install_fakes(q, s3=None):
    """Point the loaded module at fake S3 + fake gauges + a fake relational store; returns the s3 fake.
    The store is reachable for assertions via `q._store` (e.g. `q._store.labels[pid]`)."""
    fake = s3 or FakeS3()
    q._s3 = lambda: fake
    q._GAUGES = {"score": FakeGauge(), "breach": FakeGauge()}
    install_store(q)
    return fake
