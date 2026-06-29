"""Offline harness for the 013 quality-monitoring tests.

Loads `gateway/app/quality.py` standalone (its heavy imports — prometheus_client, boto3, the eval
module — are deferred, so the pure scoring/breach/cooldown logic imports with zero third-party deps).
A tiny in-memory S3 fake stands in for the MinIO `results` bucket so the prediction↔label join,
label ingestion, and the compute/report path are testable without a live store.
"""
import importlib.util
import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUALITY_PATH = os.path.join(REPO, "gateway", "app", "quality.py")


def load_quality():
    spec = importlib.util.spec_from_file_location("quality_under_test", QUALITY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClientError(Exception):
    """Mimics botocore's ClientError shape so quality._missing() can tell a 404 from a transient error."""

    def __init__(self, code="404"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Minimal in-memory S3: enough surface for quality.py's put/get/head/list over the results bucket.
    Missing keys raise a 404-shaped ClientError (like real boto3), so the not-found vs transient
    distinction in attach_label is exercised faithfully."""

    def __init__(self):
        self.objs = {}  # key -> bytes

    def put_object(self, Bucket, Key, Body, **kw):
        self.objs[Key] = Body if isinstance(Body, bytes) else bytes(Body)

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise FakeClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objs[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objs:
            raise FakeClientError("404")
        return {}

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        # single un-truncated page (no IsTruncated) — quality._list_keys terminates after one call.
        return {"Contents": [{"Key": k} for k in sorted(self.objs) if k.startswith(Prefix)]}


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


def install_fakes(q, s3=None):
    """Point the loaded module at fake S3 + fake gauges; returns the s3 fake for assertions."""
    fake = s3 or FakeS3()
    q._s3 = lambda: fake
    q._GAUGES = {"score": FakeGauge(), "breach": FakeGauge()}
    return fake
