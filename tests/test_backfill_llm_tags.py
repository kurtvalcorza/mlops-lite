"""022 T462/T477 — base-GGUF registration + the legacy task-tag backfill (offline, fake registry).

Pins: `register_base_gguf` registers each PRESENT zoo base exactly once (re-run: nothing new;
absent file: skipped, never downloaded — R7) with the tags both resolution paths need
(kind=full-model + base_id). `backfill_llm_task_tags` stamps task/serving_engine on legacy
LLM-shaped versions (ops-bot-v1/v2), is idempotent (re-run is a no-op) and NON-CLOBBER (an
existing tag is never overwritten), and never touches non-LLM versions.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from _llmregistry import FakeRegistry  # noqa: E402
from backfill_llm_task_tags import backfill  # noqa: E402
from register_base_gguf import register_bases  # noqa: E402


def _quiet(*a, **kw):
    pass


# -- T462: register_base_gguf ------------------------------------------------------------------------

def _bases(tmp_path, present=("b1.gguf",)):
    for name in present:
        (tmp_path / name).write_bytes(b"g" * 128)
    return [{"name": "qwen-b1", "base_id": "Qwen/B1", "file": "b1.gguf"},
            {"name": "qwen-b2", "base_id": "Qwen/B2", "file": "b2.gguf"}]


def _no_upload(path, bucket, key, log):
    """Stub the Garage upload seam (023 T491): these are OFFLINE tests — the default `_garage_upload`
    reaches for a live S3 endpoint, which fails on any host without the stack (CI, clean checkout)."""


def test_register_bases_registers_present_and_skips_absent(tmp_path):
    reg = FakeRegistry()
    report = register_bases(reg, bases=_bases(tmp_path), gguf_dir=str(tmp_path), log=_quiet,
                            upload=_no_upload)
    assert [r["name"] for r in report["registered"]] == ["qwen-b1"]
    assert [r["name"] for r in report["skipped"]] == ["qwen-b2"]  # absent → skipped, no download
    mv = reg.get_model_version("qwen-b1", "1")
    assert mv.tags["kind"] == "full-model" and mv.tags["task"] == "text-generation"
    # The registered source is the STORE object (022 on-HW: MLflow 3.x rejects a bare local path;
    # the agent materializes this s3:// object) — not the local zoo file the upload read from.
    assert mv.tags["base_id"] == "Qwen/B1" and mv.source == "s3://models/base-zoo/qwen-b1.gguf"


def test_register_bases_is_idempotent(tmp_path):
    reg = FakeRegistry()
    bases = _bases(tmp_path)
    register_bases(reg, bases=bases, gguf_dir=str(tmp_path), log=_quiet, upload=_no_upload)
    report = register_bases(reg, bases=bases, gguf_dir=str(tmp_path), log=_quiet,
                            upload=_no_upload)
    assert report["registered"] == []  # re-run registers nothing new
    assert len([mv for mv in reg.versions if mv.name == "qwen-b1"]) == 1


# -- T477: backfill_llm_task_tags --------------------------------------------------------------------

def _legacy_registry():
    reg = FakeRegistry()
    # the live gap: fine-tunes registered pre-022 — kind/format but NO task/serving_engine
    reg.add("ops-bot-v1", 1, "s3://models/a1.gguf",
            {"kind": "lora-adapter", "format": "gguf", "base_model": "Qwen/B1"})
    reg.add("ops-bot-v2", 1, "s3://models/a2.gguf",
            {"kind": "lora-adapter", "format": "gguf", "base_model": "Qwen/B1"})
    # already-correct 022 registrations — must NOT be re-tagged
    reg.add("ops-bot-v3", 1, "s3://models/a3.gguf",
            {"kind": "lora-adapter", "format": "gguf", "task": "text-generation",
             "serving_engine": "llama.cpp"})
    # non-LLM shape — never touched
    reg.add("vision-net", 1, "s3://models/v.pt", {"kind": "vision-classifier"})
    return reg


def test_backfill_tags_legacy_versions():
    reg = _legacy_registry()
    report = backfill(reg, log=_quiet)
    tagged = {r["name"] for r in report["tagged"]}
    assert tagged == {"ops-bot-v1", "ops-bot-v2"}
    for name in ("ops-bot-v1", "ops-bot-v2"):
        tags = reg.get_model_version(name, "1").tags
        assert tags["task"] == "text-generation" and tags["serving_engine"] == "llama.cpp"
    assert "task" not in reg.get_model_version("vision-net", "1").tags


def test_backfill_is_idempotent_and_non_clobber():
    reg = _legacy_registry()
    # a version with a DIFFERENT existing task tag must never be clobbered
    reg.add("weird", 1, "s3://models/w.gguf",
            {"kind": "lora-adapter", "task": "custom-task"})
    backfill(reg, log=_quiet)
    writes_after_first = len(reg.tag_writes)
    report = backfill(reg, log=_quiet)
    assert report["tagged"] == []                       # re-run is a no-op
    assert len(reg.tag_writes) == writes_after_first    # zero additional writes
    assert reg.get_model_version("weird", "1").tags["task"] == "custom-task"  # non-clobber
    # 'weird' got only the missing serving_engine on the first run, task untouched
    assert reg.get_model_version("weird", "1").tags["serving_engine"] == "llama.cpp"


# -- T476: the fine-tune flow stamps task descriptors at registration --------------------------------

def test_finetune_registration_stamps_task_descriptors(monkeypatch, tmp_path):
    import types

    sys.path.insert(0, os.path.join(REPO, "training", "flows"))
    import finetune

    class FakeS3:
        def put_object(self, **kw):
            pass

    captured = {}

    class FakeClient:
        def __init__(self, tracking_uri=None):
            pass

        def create_registered_model(self, name):
            pass

        def create_model_version(self, name, source, run_id, tags):
            captured.update(name=name, source=source, tags=tags)

            class MV:
                version = "7"

            return MV()

    # register_version imports `from mlflow.tracking import MlflowClient` at CALL time — stub the
    # sys.modules seam directly so this is independent of whatever mlflow state (real module or a
    # prior test's stub) the suite left behind.
    fake_tracking = types.ModuleType("mlflow.tracking")
    fake_tracking.MlflowClient = FakeClient
    fake_exceptions = types.ModuleType("mlflow.exceptions")
    fake_exceptions.MlflowException = type("MlflowException", (Exception,), {})
    monkeypatch.setitem(sys.modules, "mlflow.tracking", fake_tracking)
    monkeypatch.setitem(sys.modules, "mlflow.exceptions", fake_exceptions)
    monkeypatch.setattr(finetune, "_s3", lambda: FakeS3())
    gguf = tmp_path / "a.gguf"
    gguf.write_bytes(b"g")
    register = getattr(finetune.register_version, "fn", finetune.register_version)
    out = register("ops-bot", str(gguf), "run123", "Qwen/B1", "ops-qa", "v3")
    # FR-266: a NEW fine-tune registers as a first-class text-generation serving target
    assert captured["tags"]["task"] == "text-generation"
    assert captured["tags"]["serving_engine"] == "llama.cpp"
    assert captured["tags"]["kind"] == "lora-adapter"
    assert captured["tags"]["base_model"] == "Qwen/B1"
    assert captured["tags"]["dataset_name"] == "ops-qa"
    assert out["version"] == "7"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
