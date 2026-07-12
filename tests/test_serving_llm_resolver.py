"""022 T461/T463/T473 — the ActiveServingLLM pointer + the host-agent cold-load resolver.

Offline, GPU-free (research R8): the pointer store and the registry are the fakes in
tests/_llmregistry.py; artifacts are tmp files. Pins the serving-resolution contract:
full-model → `-m source`; lora-adapter → base resolved from lineage in ONE hop (registered name
or `base_id` tag — never an adapter chain); unset pointer → None (the configured default base);
invalid target → ResolutionError (the promote/select refusal, FR-265); unreachable infra →
ResolutionUnavailable (the env-fallback seam, never a silent guess).
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _llmregistry import FakeLLMStore, FakeRegistry  # noqa: E402

from hostagent import serving_llm  # noqa: E402
from platformlib import llmresolve, store  # noqa: E402


def _pointed_store(name="ops-bot"):
    s = FakeLLMStore()
    s.set_serving_llm(None, name, 1000.0, "operator")
    return s


def _gguf(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"g" * 1024)
    return str(p)


# -- T461: the pointer table + accessors ------------------------------------------------------------

def test_ddl_covers_the_serving_llm_pointer_table():
    from platformlib import migrations
    baseline = [m for m in migrations.discover() if m.version == 1][0].sql
    assert "CREATE TABLE IF NOT EXISTS serving_llm " in baseline  # 023 US4: files own the schema
    assert "serving_llm" in store.TABLES
    # additive-only — the schema version does not bump for a new IF NOT EXISTS table
    assert store.SCHEMA_VERSION == 1


def test_pointer_round_trip_and_unset_default():
    s = FakeLLMStore()
    assert serving_llm.active_model_name(store=s) is None  # fresh store → default base
    s.set_serving_llm(None, "ops-bot", 1000.0, "operator")
    assert serving_llm.active_model_name(store=s) == "ops-bot"
    s.clear_serving_llm(None)
    assert serving_llm.active_model_name(store=s) is None


def test_pointer_store_outage_is_unavailable_not_a_guess():
    s = FakeLLMStore()
    s.fail = True
    with pytest.raises(serving_llm.ResolutionUnavailable):
        serving_llm.active_model_name(store=s)


# -- T463: full-model resolution --------------------------------------------------------------------

def test_full_model_resolves_source_as_base(tmp_path):
    base = _gguf(tmp_path, "base.gguf")
    reg = FakeRegistry()
    reg.add("qwen-base", 1, base, {"kind": "full-model", "task": "text-generation"}, serving=True)
    out = serving_llm.resolve(store=_pointed_store("qwen-base"), client=reg)
    assert out["model_name"] == "qwen-base" and out["version"] == "1"
    assert out["base_gguf"] == base and out["adapter_gguf"] is None
    assert out["kind"] == "full-model" and out["base"] is None


def test_unset_pointer_resolves_to_none():
    assert serving_llm.resolve(store=FakeLLMStore(), client=FakeRegistry()) is None


def test_unset_pointer_adopts_single_promoted_llm(tmp_path):
    # F4: unset pointer + exactly one promoted @serving text-gen model → adopt it (the agent serves
    # what the console shows; a pre-022 promotion keeps serving), not the env default.
    base = _gguf(tmp_path, "base.gguf")
    reg = FakeRegistry()
    reg.add("qwen-base", 1, base, {"kind": "full-model", "task": "text-generation"}, serving=True)
    out = serving_llm.resolve(store=FakeLLMStore(), client=reg)
    assert out is not None and out["model_name"] == "qwen-base" and out["base_gguf"] == base


def test_unset_pointer_several_promoted_uses_env_default(tmp_path):
    b1, b2 = _gguf(tmp_path, "b1.gguf"), _gguf(tmp_path, "b2.gguf")
    reg = FakeRegistry()
    reg.add("m1", 1, b1, {"kind": "full-model", "task": "text-generation"}, serving=True)
    reg.add("m2", 1, b2, {"kind": "full-model", "task": "text-generation"}, serving=True)
    assert serving_llm.resolve(store=FakeLLMStore(), client=reg) is None  # ambiguous ⇒ env default


def test_adopted_model_that_wont_resolve_falls_back_not_outage(tmp_path):
    # An ADOPTED (unset-pointer) model whose base GGUF is missing must fall back to the env default
    # (None), NEVER raise — adoption can't brick serving into an outage. Contrast the explicit-pointer
    # path (test_missing_artifact_file_is_a_resolution_error), which fails loud per FR-265.
    reg = FakeRegistry()
    reg.add("qwen-base", 1, str(tmp_path / "gone.gguf"),
            {"kind": "full-model", "task": "text-generation"}, serving=True)
    assert serving_llm.resolve(store=FakeLLMStore(), client=reg) is None


def test_no_serving_alias_is_a_resolution_error():
    reg = FakeRegistry()
    reg.add("ops-bot", 1, "s3://models/a.gguf", {"kind": "lora-adapter"})  # nothing promoted
    with pytest.raises(serving_llm.ResolutionError, match="no @serving"):
        serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)


def test_missing_artifact_file_is_a_resolution_error(tmp_path):
    reg = FakeRegistry()
    reg.add("qwen-base", 1, str(tmp_path / "gone.gguf"), {"kind": "full-model"}, serving=True)
    with pytest.raises(serving_llm.ResolutionError, match="not found"):
        serving_llm.resolve(store=_pointed_store("qwen-base"), client=reg)


def test_registry_outage_is_unavailable_not_an_error():
    class DownClient:
        def get_model_version_by_alias(self, *a):
            raise ConnectionError("registry down")

    with pytest.raises(serving_llm.ResolutionUnavailable):
        serving_llm.resolve(store=_pointed_store("x"), client=DownClient())


def test_transport_not_found_message_is_unavailable_not_a_refusal():
    # A misconfigured MLFLOW_TRACKING_URI / reverse proxy can raise a transport error whose message
    # merely CONTAINS "not found" but carries no RESOURCE_DOES_NOT_EXIST error code — that is a
    # connectivity problem (degrade to the env default), NOT an invalid target (refuse). It must
    # propagate as ResolutionUnavailable, never ResolutionError.
    class ProxyClient:
        def get_model_version_by_alias(self, *a):
            raise RuntimeError("502 Bad Gateway: upstream not found")

    with pytest.raises(serving_llm.ResolutionUnavailable):
        serving_llm.resolve(store=_pointed_store("x"), client=ProxyClient())


def test_is_not_found_keys_only_on_the_mlflow_code():
    # unit-level guard on the narrowed classifier
    assert llmresolve._is_not_found(_llmregistry_notfound()) is True
    assert llmresolve._is_not_found(RuntimeError("upstream not found")) is False
    assert llmresolve._is_not_found(ConnectionError("registry down")) is False


def _llmregistry_notfound():
    from _llmregistry import NotFoundError
    return NotFoundError("RESOURCE_DOES_NOT_EXIST: gone")


# -- T473: the adapter (base + LoRA) path -----------------------------------------------------------

def test_adapter_resolves_base_by_registered_name(tmp_path):
    base, adapter = _gguf(tmp_path, "base.gguf"), _gguf(tmp_path, "adapter.gguf")
    reg = FakeRegistry()
    reg.add("qwen-base", 1, base, {"kind": "full-model"}, serving=True)
    reg.add("ops-bot", 3, adapter,
            {"kind": "lora-adapter", "base_model": "qwen-base"}, serving=True)
    out = serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)
    assert out["base_gguf"] == base and out["adapter_gguf"] == adapter
    assert out["base"] == {"name": "qwen-base", "version": "1", "source": base}


def test_adapter_resolves_raw_hf_base_via_base_id_tag(tmp_path):
    # The trainer stamps the RAW HF id (Qwen/…) — matched via the registered base's base_id tag
    # (scripts/register_base_gguf.py), not a registered-model name.
    base, adapter = _gguf(tmp_path, "base.gguf"), _gguf(tmp_path, "adapter.gguf")
    reg = FakeRegistry()
    reg.add("qwen2.5-0.5b-instruct", 1, base,
            {"kind": "full-model", "base_id": "Qwen/Qwen2.5-0.5B-Instruct"})
    reg.add("ops-bot", 2, adapter,
            {"kind": "lora-adapter", "base_model": "Qwen/Qwen2.5-0.5B-Instruct"}, serving=True)
    out = serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)
    assert out["base_gguf"] == base and out["adapter_gguf"] == adapter


def test_legacy_adapter_without_kind_is_inferred(tmp_path):
    # FR-267: a pre-022 fine-tune (base_model + format=gguf, no kind) still serves as an adapter.
    base, adapter = _gguf(tmp_path, "base.gguf"), _gguf(tmp_path, "adapter.gguf")
    reg = FakeRegistry()
    reg.add("qwen-base", 1, base, {"kind": "full-model"})
    reg.add("ops-bot", 1, adapter, {"format": "gguf", "base_model": "qwen-base"}, serving=True)
    out = serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)
    assert out["kind"] == "lora-adapter" and out["adapter_gguf"] == adapter


def test_missing_base_is_a_resolution_error(tmp_path):
    reg = FakeRegistry()
    reg.add("ops-bot", 1, _gguf(tmp_path, "a.gguf"),
            {"kind": "lora-adapter", "base_model": "nowhere-base"}, serving=True)
    with pytest.raises(serving_llm.ResolutionError, match="register the local base"):
        serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)


def test_adapter_chain_is_refused_not_walked(tmp_path):
    # contracts/serving-resolution.md: base_model MUST resolve DIRECTLY to a full-model version —
    # an adapter pointing at another adapter is an error, never a multi-hop chain (PR #64 R2).
    reg = FakeRegistry()
    reg.add("mid-adapter", 1, _gguf(tmp_path, "mid.gguf"),
            {"kind": "lora-adapter", "base_model": "qwen-base"})
    reg.add("ops-bot", 1, _gguf(tmp_path, "top.gguf"),
            {"kind": "lora-adapter", "base_model": "mid-adapter"}, serving=True)
    with pytest.raises(serving_llm.ResolutionError, match="no adapter chains"):
        serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)


def test_adapter_without_base_lineage_is_refused(tmp_path):
    reg = FakeRegistry()
    reg.add("ops-bot", 1, _gguf(tmp_path, "a.gguf"), {"kind": "lora-adapter"}, serving=True)
    with pytest.raises(serving_llm.ResolutionError, match="no base_model lineage"):
        serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)


def test_base_prefers_serving_alias_then_newest_full_model(tmp_path):
    g1, g2, adapter = _gguf(tmp_path, "v1.gguf"), _gguf(tmp_path, "v2.gguf"), \
        _gguf(tmp_path, "ad.gguf")
    reg = FakeRegistry()
    reg.add("qwen-base", 1, g1, {"kind": "full-model"}, serving=True)  # @serving on v1
    reg.add("qwen-base", 2, g2, {"kind": "full-model"})                # newer, not promoted
    reg.add("ops-bot", 1, adapter,
            {"kind": "lora-adapter", "base_model": "qwen-base"}, serving=True)
    out = serving_llm.resolve(store=_pointed_store("ops-bot"), client=reg)
    assert out["base"]["version"] == "1"  # the promoted base wins over the newer version


# -- llmresolve unit seams ---------------------------------------------------------------------------

def test_task_from_kind_infers_only_llm_shapes():
    assert llmresolve.task_from_kind({"kind": "lora-adapter"}) == "text-generation"
    assert llmresolve.task_from_kind({"format": "gguf"}) == "text-generation"
    assert llmresolve.task_from_kind({"kind": "vision-classifier"}) is None
    assert llmresolve.task_from_kind({}) is None


def test_effective_kind_legacy_inference():
    assert llmresolve.effective_kind({"kind": "full-model"}) == "full-model"
    assert llmresolve.effective_kind({"kind": "lora-adapter"}) == "lora-adapter"
    assert llmresolve.effective_kind(
        {"base_model": "x", "format": "gguf"}) == "lora-adapter"  # the pre-022 fine-tune shape
    assert llmresolve.effective_kind({}) == "full-model"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
