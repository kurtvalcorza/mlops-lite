"""022 T464/T467/T470/T471/T474/T478/T479 — the gateway half of registry-driven LLM serving.

Offline (research R8): the MLflow client and the pointer store are the fakes in
tests/_llmregistry.py. Pins:

  - the ActiveServingLLM pointer accessors (T464) — round-trip, unset default, fail-loud write;
  - `llm_target_info` (T474): an adapter whose base does not resolve is refused BEFORE the gate,
    so the alias and the currently-served LLM stay unchanged (FR-265);
  - the promote route wiring (T467): promote = go live — pointer write + agent reload, operator
    `preempt` passed through; a gate-blocked promote moves nothing;
  - `resolve_serving_target`/`list_tasks` kind fallback (T478, FR-267) + kind/lineage exposure and
    the FR-276 active-pointer dedup (T479): two promoted LLM models → exactly ONE live target;
  - honest identity consumption (T470/T471, FR-260/261/262): serving state + the /infer response +
    the logged prediction all carry the AGENT-reported identity, degrading to `unknown`.
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _llmregistry import FakeLLMStore, FakeRegistry  # noqa: E402
from app import registry, serving  # noqa: E402
from app.routers import infer as infer_router  # noqa: E402
from app.routers import models as models_router  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture
def fake_reg(monkeypatch):
    reg = FakeRegistry()
    monkeypatch.setattr(registry, "_client", lambda: reg)
    return reg


@pytest.fixture
def fake_store(monkeypatch):
    s = FakeLLMStore()
    monkeypatch.setattr(registry, "_store", lambda: s)
    return s


# -- T464: pointer accessors -------------------------------------------------------------------------

def test_pointer_round_trip_and_default(fake_reg, fake_store):
    assert registry.get_serving_llm() is None
    assert registry.active_serving_llm_name() == registry.DEFAULT_LLM  # unset + nothing promoted
    registry.set_serving_llm("ops-bot", actor="operator")
    assert registry.get_serving_llm()["model_name"] == "ops-bot"
    assert registry.active_serving_llm_name() == "ops-bot"


# -- F4: adopt the sole promoted @serving LLM when the pointer is unset -------------------------------

def test_active_name_adopts_single_promoted_llm_when_unset(fake_reg, fake_store):
    fake_reg.add("ops-bot", 2, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen", "task": "text-generation"},
                 serving=True)
    assert registry.get_serving_llm() is None
    assert registry.active_serving_llm_name() == "ops-bot"  # adopted, not the stale default


def test_active_name_defaults_when_several_promoted_and_unset(fake_reg, fake_store):
    fake_reg.add("qwen", 5, "/zoo/b.gguf",
                 {"kind": "full-model", "task": "text-generation"}, serving=True)
    fake_reg.add("ops-bot", 2, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen", "task": "text-generation"},
                 serving=True)
    assert registry.active_serving_llm_name() == registry.DEFAULT_LLM  # ambiguous ⇒ default


def test_active_name_prefers_explicit_pointer_over_adoption(fake_reg, fake_store):
    fake_reg.add("ops-bot", 2, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen", "task": "text-generation"},
                 serving=True)
    fake_store.set_serving_llm(None, "qwen", 1000.0, "operator")
    assert registry.active_serving_llm_name() == "qwen"  # the pointer wins over the promoted alias


def test_active_name_on_store_outage_is_default_not_adopted(fake_reg, fake_store):
    # A store OUTAGE must return the default (matching the agent's env fallback on the same outage),
    # NOT be mistaken for an unset pointer and adopt a promoted model the agent isn't serving.
    fake_reg.add("ops-bot", 2, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen", "task": "text-generation"},
                 serving=True)
    fake_store.fail = True
    assert registry.active_serving_llm_name() == registry.DEFAULT_LLM


def test_list_tasks_ambiguous_unset_advertises_no_live_llm(fake_reg, fake_store):
    # Several promoted LLMs + unset pointer ⇒ active resolves to the default (not among them); rather
    # than show an arbitrary/nondeterministic one, list_tasks advertises NO live LLM (operator
    # promotes to disambiguate). The agent serves the env default with nothing pointer-selected.
    fake_reg.add("m1", 1, "/zoo/b1.gguf", {"kind": "full-model", "task": "text-generation"},
                 serving=True)
    fake_reg.add("m2", 1, "/zoo/b2.gguf", {"kind": "full-model", "task": "text-generation"},
                 serving=True)
    live = [t["model"] for t in registry.list_tasks() if t["task"] == "text-generation"]
    assert live == []


def test_list_tasks_single_stale_alias_is_kept_only_when_active(fake_reg, fake_store):
    # F4: one promoted LLM, unset pointer → adoption makes it the active model → it IS shown (not
    # dropped). Then point elsewhere → it's no longer the live target and is filtered out.
    fake_reg.add("qwen", 5, "/zoo/b.gguf", {"kind": "full-model"})  # registered, NOT promoted
    fake_reg.add("ops-bot", 2, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen", "task": "text-generation"},
                 serving=True)
    live = [t["model"] for t in registry.list_tasks() if t["task"] == "text-generation"]
    assert live == ["ops-bot"]  # adopted → shown as the single live LLM


def test_pointer_write_fails_loud_read_fails_soft(fake_store):
    fake_store.fail = True
    assert registry.get_serving_llm() is None  # read degrades to 'unset' (default base)
    with pytest.raises(registry.RegistryError, match="pointer write failed"):
        registry.set_serving_llm("ops-bot")


def test_pointer_write_bootstraps_the_table_first(fake_store):
    # Codex F2: an upgraded PG volume never re-ran init.sql, so the write must apply the additive
    # DDL (idempotent) before inserting — else the first promote fails with
    # `relation "serving_llm" does not exist` after already moving the alias.
    registry.set_serving_llm("ops-bot")
    assert fake_store.bootstrapped >= 1 and fake_store.row["model_name"] == "ops-bot"


# -- T474: pre-promotion resolution check ------------------------------------------------------------

def test_target_info_none_for_non_llm(fake_reg):
    fake_reg.add("vision-net", 1, "s3://models/v.pt", {"task": "image-classification"})
    assert registry.llm_target_info("vision-net", "1") is None


def test_target_info_resolves_adapter_base(fake_reg):
    fake_reg.add("qwen-base", 1, "/zoo/base.gguf", {"kind": "full-model"})
    fake_reg.add("ops-bot", 2, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen-base", "task": "text-generation"})
    info = registry.llm_target_info("ops-bot", "2")
    assert info["kind"] == "lora-adapter" and info["error"] is None
    assert info["base"]["name"] == "qwen-base" and info["base"]["source"] == "/zoo/base.gguf"


def test_target_info_unresolvable_base_carries_error(fake_reg):
    fake_reg.add("ops-bot", 1, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "nowhere", "task": "text-generation"})
    info = registry.llm_target_info("ops-bot", "1")
    assert info["error"] and "register the local base" in info["error"]


def test_target_info_recognizes_legacy_untagged_adapter(fake_reg):
    # FR-267: kind/format only, no task tag — still a text-generation target (T478 inference).
    fake_reg.add("qwen-base", 1, "/zoo/base.gguf", {"kind": "full-model"})
    fake_reg.add("ops-bot-v1", 1, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen-base"})
    info = registry.llm_target_info("ops-bot-v1", "1")
    assert info is not None and info["task"] == "text-generation" and info["error"] is None


# -- T467: promote = go live -------------------------------------------------------------------------

def _promote_env(monkeypatch, *, target, promoted=True, reload_result=None, prior=None):
    calls = {"promote": [], "pointer": [], "reload": [], "restore": []}
    monkeypatch.setattr(registry, "list_versions",
                        lambda name: [{"version": "2"}])
    monkeypatch.setattr(registry, "llm_target_info", lambda n, v: target)
    monkeypatch.setattr(registry, "promote",
                        lambda n, v, override=False:
                        calls["promote"].append((n, v)) or
                        {"name": n, "serving_version": v, "promoted": promoted,
                         "verdict": {"verdict": "pass"}})
    monkeypatch.setattr(registry, "get_serving_llm", lambda: prior)  # the pre-promote pointer
    monkeypatch.setattr(registry, "set_serving_llm",
                        lambda n, actor="operator": calls["pointer"].append(n) or
                        {"model_name": n})
    monkeypatch.setattr(registry, "restore_serving_llm",
                        lambda p: calls["restore"].append(p))
    monkeypatch.setattr(serving, "request_llm_reload",
                        lambda preempt=False: calls["reload"].append(preempt) or
                        (reload_result or {"status": "reloaded"}))
    return calls


def test_promote_llm_writes_pointer_and_requests_reload(monkeypatch):
    target = {"task": "text-generation", "kind": "lora-adapter",
              "base": {"name": "qwen-base", "version": "1", "source": "/zoo/b.gguf"},
              "error": None}
    calls = _promote_env(monkeypatch, target=target)
    res = models_router.promote("ops-bot", models_router.PromoteRequest(version="2", preempt=True))
    assert calls["promote"] == [("ops-bot", "2")]
    assert calls["pointer"] == ["ops-bot"]          # promote IS the go-live action
    assert calls["reload"] == [True]                # operator confirm passed through (FR-258)
    assert res["serving_llm"]["reload"]["status"] == "reloaded"


def test_promote_llm_rolls_back_pointer_when_target_unloadable(monkeypatch):
    # FR-265: the registry-level pre-check passed (a full-model base resolves) but the AGENT can't
    # load the artifact (GGUF absent on the host) → the reload returns `unresolvable`. The alias
    # moved, but the pointer MUST be rolled back to its prior value: left pointed at the unloadable
    # model it would 503 the next cold load (after the current model idle-reaps). The served LLM
    # stays unchanged, as FR-265 promises — beyond just the immediate reload.
    target = {"task": "text-generation", "kind": "full-model", "base": None, "error": None}
    calls = _promote_env(
        monkeypatch, target=target, prior={"model_name": "qwen", "selected_by": "operator"},
        reload_result={"status": "deferred", "unresolvable": True,
                       "reason": "serving-LLM target not loadable: model GGUF not found"})
    res = models_router.promote("ops-bot", models_router.PromoteRequest(version="2", preempt=True))
    assert calls["pointer"] == ["ops-bot"]                                    # promote wrote it …
    assert calls["restore"] == [{"model_name": "qwen", "selected_by": "operator"}]  # … then reverted
    assert res["serving_llm"]["rolled_back"] is True
    assert res["serving_llm"]["active"] == "qwen"                             # served LLM unchanged


def test_promote_llm_keeps_pointer_on_retryable_deferral(monkeypatch):
    # A job-holder / missing-confirm deferral is RETRYABLE (not `unresolvable`) — the pointer stays,
    # so a later reload or a re-promote with preempt goes live. Only an unloadable target rolls back.
    target = {"task": "text-generation", "kind": "full-model", "base": None, "error": None}
    calls = _promote_env(
        monkeypatch, target=target,
        reload_result={"status": "deferred",
                       "reason": "would displace resident vision — confirmation required"})
    res = models_router.promote("ops-bot", models_router.PromoteRequest(version="2"))
    assert calls["pointer"] == ["ops-bot"] and calls["restore"] == []        # kept — retryable
    assert res["serving_llm"]["active"] == "ops-bot"


def test_promote_refuses_unresolvable_llm_before_the_gate(monkeypatch):
    target = {"task": "text-generation", "kind": "lora-adapter", "base": None,
              "error": "base 'x' is not a registered full-model version"}
    calls = _promote_env(monkeypatch, target=target)
    with pytest.raises(HTTPException) as exc:
        models_router.promote("ops-bot", models_router.PromoteRequest(version="2"))
    assert exc.value.status_code == 409 and "refused" in exc.value.detail
    assert calls["promote"] == [] and calls["pointer"] == []  # alias + served LLM unchanged


def test_promote_non_llm_touches_no_pointer(monkeypatch):
    calls = _promote_env(monkeypatch, target=None)
    res = models_router.promote("vision-net", models_router.PromoteRequest(version="2"))
    assert calls["promote"] and calls["pointer"] == [] and calls["reload"] == []
    assert "serving_llm" not in res


def test_gate_blocked_promote_moves_nothing(monkeypatch):
    target = {"task": "text-generation", "kind": "full-model", "base": None, "error": None}
    calls = _promote_env(monkeypatch, target=target, promoted=False)
    models_router.promote("qwen", models_router.PromoteRequest(version="2"))
    assert calls["pointer"] == [] and calls["reload"] == []  # blocked verdict → not live


# -- T478/T479: task surfaces ------------------------------------------------------------------------

def test_list_tasks_infers_task_and_exposes_kind_lineage(fake_reg, fake_store):
    fake_reg.add("ops-bot", 1, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen-base",
                  "dataset_name": "ops-qa", "dataset_version": "v3"}, serving=True)
    tasks = registry.list_tasks()
    entry = next(t for t in tasks if t["model"] == "ops-bot")
    assert entry["task"] == "text-generation"        # inferred, not null → a real panel (FR-267)
    assert entry["kind"] == "lora-adapter"
    assert entry["lineage"]["base_model"] == "qwen-base"
    assert entry["lineage"]["dataset_name"] == "ops-qa"


def test_list_tasks_filters_to_the_active_pointer(fake_reg, fake_store):
    # FR-276: a promote moves only its own model's alias, so BOTH models keep one — only the
    # active-pointer model may appear as the live text-generation target (no stale duplicate).
    fake_reg.add("qwen", 5, "/zoo/base.gguf",
                 {"kind": "full-model", "task": "text-generation"}, serving=True)
    fake_reg.add("ops-bot", 2, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen", "task": "text-generation"},
                 serving=True)
    def live_llms():
        return [t["model"] for t in registry.list_tasks() if t["task"] == "text-generation"]

    fake_store.set_serving_llm(None, "ops-bot", 1000.0, "operator")
    assert live_llms() == ["ops-bot"]
    # switch back → the other model is the single live target again, no stale duplicate
    fake_store.set_serving_llm(None, "qwen", 1001.0, "operator")
    assert live_llms() == ["qwen"]


def test_resolve_serving_target_falls_back_to_kind(fake_reg):
    # A legacy promoted LLM with NO task tag is invisible to the tag search — the kind fallback
    # still routes it (T478).
    fake_reg.add("ops-bot-v1", 1, "s3://models/a.gguf",
                 {"kind": "lora-adapter", "base_model": "qwen-base"}, serving=True)
    target = registry.resolve_serving_target("text-generation")
    assert target is not None and target["name"] == "ops-bot-v1"
    assert target["serving_engine"] == "llama.cpp"


# -- T470/T471: honest identity consumption ----------------------------------------------------------

def test_identity_from_health_maps_agent_fields():
    ident = serving._identity_from_health(
        {"model_name": "ops-bot", "registry_version": "3",
         "base": "base.gguf", "adapter": "a.gguf", "model": "ops-bot"})
    assert ident == {"serving_model": "ops-bot", "serving_version": "3",
                     "base": "base.gguf", "adapter": "a.gguf"}


def test_identity_degrades_to_unknown_never_a_config_guess():
    assert serving._identity_from_health({})["serving_model"] == "unknown"
    assert serving._UNKNOWN_IDENTITY["serving_version"] is None


def test_infer_attributes_response_and_prediction_to_agent_identity(monkeypatch):
    logged = []

    async def fake_health():
        return True

    async def fake_run(prompt, max_tokens, temperature, *, preempt=False):
        return {"text": "hi", "load_ms": 0.0, "infer_ms": 1.0, "model": "ops-bot", "usage": {}}

    async def fake_identity():
        return {"serving_model": "ops-bot", "serving_version": "3",
                "base": "base.gguf", "adapter": "a.gguf"}

    monkeypatch.setattr(infer_router, "health", fake_health)
    monkeypatch.setattr(infer_router, "run_inference", fake_run)
    monkeypatch.setattr(infer_router, "llm_identity", fake_identity)
    monkeypatch.setattr(infer_router.quality, "log_prediction",
                        lambda model, version, modality, *a, **kw:
                        logged.append((model, version, modality)) or "pid-1")
    monkeypatch.setattr(infer_router.quality, "capture_input", lambda *a, **kw: None)
    monkeypatch.setattr(infer_router.tracing, "emit", lambda **kw: None)

    res = asyncio.run(infer_router.infer(infer_router.InferRequest(prompt="x")))
    # FR-262: response identity == logged identity == the agent-reported served identity
    assert res["registry_model"] == "ops-bot" and res["registry_version"] == "3"
    assert logged == [("ops-bot", "3", "text-generation")]


def test_stream_log_uses_agent_identity_not_config():
    # Source pin (read from disk — import-order-independent): the detached _log resolves the
    # served identity from the agent; the old SERVING_MODEL config-guess is gone.
    path = os.path.join(REPO, "gateway", "app", "routers", "stream.py")
    src = open(path, encoding="utf-8").read()
    assert "llm_identity" in src
    assert "log_prediction(serving.SERVING_MODEL" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
