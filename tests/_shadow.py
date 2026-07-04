"""Offline harness for the 016 shadow-replay tests (018 US4 store-backed).

Loads `gateway/app/shadow.py` as a member of the SAME synthetic package as its configured `quality`
module (so shadow's `from . import quality` binds to the FakeS3+FakeStore-wired instance). The window
join, verdict math, and the US3 guards run with no live store and no GPU. Seeds write BOTH the object
bodies (input/prediction) into FakeS3 AND the relational index rows (predictions/labels/capture_index)
into the FakeStore, since `resolve_window` now joins the store and reads only the ≤window_n bodies.
"""
import json

from _pkgload import load_in_package, register_sibling
from _quality import FakeS3, FakeStore, install_store, load_quality  # noqa: F401 (re-exported)


class FakeS3Del(FakeS3):
    """FakeS3 already carries delete_object; kept as a named alias for the shadow tests' imports."""


def load_shadow(quality_mod):
    """Load shadow.py into quality_mod's package so its `from . import quality` resolves to it."""
    pkg = quality_mod.__package__
    register_sibling(pkg, "quality", quality_mod)
    return load_in_package(pkg, "shadow")


def make_quality(s3, **flags):
    """A configured quality module: FakeS3 store + FakeStore index + capture flags set for the test."""
    q = load_quality()
    q._s3 = lambda: s3
    install_store(q)  # a fresh FakeStore, wired as quality._store
    q.QUALITY_CAPTURE_IO = flags.get("capture", True)
    q.QUALITY_LOGGING_ENABLED = flags.get("logging", True)
    # TTL off by default so the window/verdict tests (tiny absolute timestamps) exercise the join, not
    # expiry; the dedicated TTL test drives resolve_window(now=, ttl_s=) explicitly.
    q.SHADOW_CAPTURE_TTL_S = flags.get("ttl_s", 0)
    return q


def _dt(ts):
    """A tz-aware datetime for a float epoch ts (the store columns are timestamptz)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc)


def seed_input(s3, q, modality, pid, payload, ts):
    """The recoverable input: its body object in S3 + a capture_index row in the store."""
    key = q._input_key(modality, pid, ts)
    mod = q.normalize_modality(modality)
    s3.put_object(Bucket="results", Key=key,
                  Body=_json({"prediction_id": pid, "modality": mod, "input": payload, "ts": ts}))
    q._store.capture_input(q._conn(), pid, mod, key, _dt(ts))


def seed_prediction(s3, q, pid, *, name, version, modality, prediction, ts):
    """The champion prediction: its OUTPUT body object in S3 + a predictions row in the store (a None
    prediction is a streamed row — logged with streamed=True, excluded from scoring by the readers)."""
    ref = f"{q.PRED_PREFIX}{pid}.json"
    mod = q.normalize_modality(modality)
    s3.put_object(Bucket="results", Key=ref,
                  Body=_json({"prediction_id": pid, "model_name": name, "model_version": str(version),
                              "modality": mod, "prediction": prediction, "ts": ts}))
    q._store.log_prediction(q._conn(), pid, name, str(version), mod, _dt(ts),
                            streamed=(prediction is None), payload_ref=ref)


def seed_label(s3, q, pid, label, ts=0.0):
    """The ground-truth label — fully relational now (a labels row; no object)."""
    q._store.attach_label(q._conn(), pid, label, _dt(ts))


def _json(obj):
    return json.dumps(obj).encode()
