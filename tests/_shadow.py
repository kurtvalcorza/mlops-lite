"""Offline harness for the 016 shadow-replay tests.

Loads `gateway/app/shadow.py` standalone, wired to a configured `quality` module (the same in-memory
FakeS3-backed instance the 013 tests use) so the window join, verdict math, and the US3 guards run with
no live store and no GPU. `shadow.py` falls back from `from . import quality` to `import quality`, so we
register the configured quality under `sys.modules["quality"]` before loading shadow.
"""
import importlib.util
import os
import sys

from _quality import FakeS3, load_quality  # noqa: F401  (re-exported for the tests)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "gateway", "app")


class FakeS3Del(FakeS3):
    def delete_object(self, Bucket, Key):
        self.objs.pop(Key, None)


def load_shadow(quality_mod):
    """Load shadow.py bound to `quality_mod` (its `import quality` resolves to this configured module)."""
    sys.modules["quality"] = quality_mod
    if APP not in sys.path:
        sys.path.insert(0, APP)
    spec = importlib.util.spec_from_file_location("shadow_under_test", os.path.join(APP, "shadow.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_quality(s3, **flags):
    """A configured quality module: FakeS3 store + capture flags set for the test."""
    q = load_quality()
    q._s3 = lambda: s3
    q.QUALITY_CAPTURE_IO = flags.get("capture", True)
    q.QUALITY_LOGGING_ENABLED = flags.get("logging", True)
    return q


def seed_input(s3, q, modality, pid, payload, ts):
    s3.put_object(Bucket="results", Key=q._input_key(modality, pid, ts),
                  Body=_json({"prediction_id": pid, "modality": q.normalize_modality(modality),
                              "input": payload, "ts": ts}))


def seed_prediction(s3, q, pid, *, name, version, modality, prediction, ts):
    s3.put_object(Bucket="results", Key=f"{q.PRED_PREFIX}{pid}.json",
                  Body=_json({"prediction_id": pid, "model_name": name, "model_version": str(version),
                              "modality": q.normalize_modality(modality), "prediction": prediction,
                              "ts": ts}))


def seed_label(s3, q, pid, label, ts=0.0):
    s3.put_object(Bucket="results", Key=f"{q.LABEL_PREFIX}{pid}.json",
                  Body=_json({"prediction_id": pid, "label": label, "ts": ts}))


def _json(obj):
    import json
    return json.dumps(obj).encode()
