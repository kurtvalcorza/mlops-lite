"""022 T462 (on-HW fix): `register_base_gguf.register_bases` uploads each base GGUF to the object
store and registers an `s3://` source — NOT a bare local path, which MLflow 3.x rejects
(`Invalid model version source … run_id request parameter has to be specified`). Offline against a
fake registry + an injected upload (no live Garage/MLflow), so the upload + idempotency are pinned
without the object store the earlier local-path source silently depended on failing.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "scripts"), os.path.join(REPO, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import register_base_gguf as rbg  # noqa: E402
from _llmregistry import FakeRegistry  # noqa: E402


def _base(tmp_path, name="qwen2.5-0.5b-instruct", make=True):
    f = tmp_path / f"{name}.gguf"
    if make:
        f.write_bytes(b"GGUF" + b"\0" * 128)
    return {"name": name, "base_id": "Qwen/Qwen2.5-0.5B-Instruct", "file": f.name}


def test_registers_an_s3_source_and_uploads_once(tmp_path):
    reg = FakeRegistry()
    uploads = []
    rep = rbg.register_bases(reg, bases=[_base(tmp_path)], gguf_dir=str(tmp_path),
                             upload=lambda p, b, k, log=None: uploads.append((b, k)))
    assert len(rep["registered"]) == 1
    # the version source is the s3:// object, never a local filesystem path (MLflow 3.x rejects that)
    assert rep["registered"][0]["source"] == "s3://models/base-zoo/qwen2.5-0.5b-instruct.gguf"
    assert uploads == [("models", "base-zoo/qwen2.5-0.5b-instruct.gguf")]  # uploaded exactly once
    mv = reg.search_model_versions("name='qwen2.5-0.5b-instruct'")[0]
    assert mv.tags["kind"] == "full-model" and mv.tags["format"] == "gguf"
    assert mv.tags["base_id"] == "Qwen/Qwen2.5-0.5B-Instruct"  # so slashed-id lineage resolves


def test_rerun_is_idempotent_no_reupload_no_new_version(tmp_path):
    reg = FakeRegistry()
    uploads = []
    up = lambda p, b, k, log=None: uploads.append(k)  # noqa: E731
    base = _base(tmp_path)
    rbg.register_bases(reg, bases=[base], gguf_dir=str(tmp_path), upload=up)
    rep2 = rbg.register_bases(reg, bases=[base], gguf_dir=str(tmp_path), upload=up)
    assert rep2["registered"] == [] and rep2["skipped"][0]["reason"] == "already registered"
    assert len(uploads) == 1  # the (name, s3-source) already exists → no second upload/registration
    assert len(reg.search_model_versions("name='qwen2.5-0.5b-instruct'")) == 1


def test_absent_gguf_is_skipped_and_never_uploaded(tmp_path):
    reg = FakeRegistry()
    uploads = []
    rep = rbg.register_bases(reg, bases=[_base(tmp_path, make=False)], gguf_dir=str(tmp_path),
                             upload=lambda *a, **k: uploads.append(1))
    assert rep["registered"] == [] and "no GGUF" in rep["skipped"][0]["reason"]
    assert uploads == []  # nothing downloaded/uploaded for a base that isn't in the local zoo


class _FakeS3:
    """The tiny boto3-client surface `_garage_upload` touches — head_object + upload_file."""

    def __init__(self, present_size=None):
        self.present_size = present_size   # None ⇒ object absent (head_object raises)
        self.uploaded = []

    def head_object(self, Bucket, Key):
        if self.present_size is None:
            raise RuntimeError("404 NoSuchKey")
        return {"ContentLength": self.present_size}

    def upload_file(self, path, bucket, key):
        self.uploaded.append((bucket, key))


def test_garage_upload_skips_present_uploads_absent_reuploads_on_size_mismatch(tmp_path, monkeypatch):
    # Exercise the REAL _garage_upload head/size branch (@claude review) — not just the injected
    # stand-in the register_bases tests use. platformlib.store.s3_client is imported inside the fn,
    # so monkeypatching it swaps in the fake without needing boto3/a live store.
    f = tmp_path / "b.gguf"
    f.write_bytes(b"x" * 200)
    quiet = lambda *a, **k: None  # noqa: E731

    present = _FakeS3(present_size=200)                       # same size → skip
    monkeypatch.setattr("platformlib.store.s3_client", lambda: present)
    rbg._garage_upload(str(f), "models", "base-zoo/b.gguf", log=quiet)
    assert present.uploaded == []

    absent = _FakeS3(present_size=None)                       # not present → upload
    monkeypatch.setattr("platformlib.store.s3_client", lambda: absent)
    rbg._garage_upload(str(f), "models", "base-zoo/b.gguf", log=quiet)
    assert absent.uploaded == [("models", "base-zoo/b.gguf")]

    mismatch = _FakeS3(present_size=999)                      # present but wrong size → re-upload
    monkeypatch.setattr("platformlib.store.s3_client", lambda: mismatch)
    rbg._garage_upload(str(f), "models", "base-zoo/b.gguf", log=quiet)
    assert mismatch.uploaded == [("models", "base-zoo/b.gguf")]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
