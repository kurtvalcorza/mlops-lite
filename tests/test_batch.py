"""014 US1 — offline batch-inference core (T267 / SC-081).

Pure, offline coverage of the scoring loop + content-addressed write (injected predict_fn + a fake S3):
one output per input row, record-and-continue on per-row failures, abort-threshold breach, idempotent
content-addressed results, and fail-fast on an empty/unreadable dataset. The GPU-lease discipline
(acquire-once/release) lives in the native flow and is asserted on hardware (SC-082).
"""
import importlib.util
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "batch_ut", os.path.join(REPO, "gateway", "app", "batch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


b = _load()


class FakeS3:
    def __init__(self, objs=None):
        self.objs = dict(objs or {})

    def put_object(self, Bucket, Key, Body, **kw):
        self.objs[(Bucket, Key)] = Body if isinstance(Body, bytes) else bytes(Body)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objs:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}


def _echo(row):
    return f"scored:{row.get('instruction', row)}"


# --- run_batch -------------------------------------------------------------------------------------

def test_one_output_per_input_row():
    rows = [{"instruction": f"q{i}"} for i in range(10)]
    out = b.run_batch(rows, _echo)
    assert out["n_in"] == 10 and out["n_out"] == 10 and out["n_failed"] == 0
    assert len(out["results"]) == 10              # exactly one result per input (SC-081)
    assert all(r["ok"] and r["output"].startswith("scored:") for r in out["results"])


def test_per_row_failure_is_recorded_and_batch_continues():
    rows = [{"instruction": f"q{i}"} for i in range(10)]

    def flaky(row):
        if row["instruction"] == "q3":
            raise ValueError("bad row")
        return "ok"
    out = b.run_batch(rows, flaky)
    assert out["n_in"] == 10 and out["n_out"] == 9 and out["n_failed"] == 1
    bad = [r for r in out["results"] if not r["ok"]]
    assert len(bad) == 1 and bad[0]["index"] == 3 and "bad row" in bad[0]["error"]


def test_abort_threshold_breach_raises():
    rows = [{"instruction": f"q{i}"} for i in range(10)]  # everything fails → >50% → abort
    try:
        b.run_batch(rows, lambda r: (_ for _ in ()).throw(RuntimeError("boom")), abort_threshold=0.5)
    except b.BatchError as e:
        assert "aborted" in str(e)
    else:
        raise AssertionError("expected BatchError on abort-threshold breach")


def test_empty_dataset_fails_fast():
    try:
        b.run_batch([], _echo)
    except b.BatchError:
        pass
    else:
        raise AssertionError("expected BatchError on empty dataset")


# --- content-addressed write -----------------------------------------------------------------------

def test_write_result_is_content_addressed_and_idempotent():
    s3 = FakeS3()
    batch = b.run_batch([{"instruction": "q"}], _echo)
    w1 = b.write_result("job", batch, s3=s3)
    w2 = b.write_result("job", batch, s3=s3)               # identical results → same version
    assert w1["version"] == w2["version"]
    assert w1["result_uri"].endswith(f"batch/job/{w1['version']}/data")
    # both data + manifest landed in the results bucket.
    keys = {k for (_, k) in s3.objs}
    assert f"batch/job/{w1['version']}/data" in keys
    assert f"batch/job/{w1['version']}/manifest.json" in keys
    man = json.loads(s3.objs[(b.RESULTS_BUCKET, f"batch/job/{w1['version']}/manifest.json")])
    assert man["n_in"] == 1 and man["n_out"] == 1 and man["sha256"]


# --- score_dataset end-to-end (fake store + injected predictor) -----------------------------------

def test_score_dataset_end_to_end():
    rows = [{"instruction": f"q{i}", "response": f"a{i}"} for i in range(8)]
    raw = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    s3 = FakeS3({("datasets", "ds/v1/data"): raw})
    res = b.score_dataset("ds", "v1", "qwen@serving", predict_fn=_echo, s3=s3)
    assert res["status"] == "succeeded"
    assert res["n_in"] == 8 and res["n_out"] == 8 and res["n_failed"] == 0
    assert res["result_uri"].startswith(f"s3://{b.RESULTS_BUCKET}/batch/ds_v1/")
    # the manifest records the source dataset + model.
    man_key = next(k for (_, k) in s3.objs if k.endswith("manifest.json"))
    man = json.loads(s3.objs[(b.RESULTS_BUCKET, man_key)])
    assert man["dataset"] == "ds@v1" and man["model"] == "qwen@serving"


def test_score_dataset_empty_fails_fast_no_artifact():
    s3 = FakeS3({("datasets", "empty/v1/data"): b"\n"})
    try:
        b.score_dataset("empty", "v1", "m", predict_fn=_echo, s3=s3)
    except b.BatchError:
        pass
    else:
        raise AssertionError("expected BatchError on empty dataset")
    assert not any(k.startswith("batch/") for (_, k) in s3.objs)  # no partial/half-written artifact


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
