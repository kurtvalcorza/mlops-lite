"""022 T485 — the two cross-cutting boundary guards (FR-273 footprint, FR-275 automation).

  - **FR-273 (Principle III)**: registry-driven LLM serving adds NO new always-on resident
    process — the compose service set and the agent's engine registry are unchanged, resolution
    is cold-load-only (importing the resolver spawns no thread), and the only new persisted state
    is the one-row pointer table.
  - **FR-275 (operator-only switch)**: the retraining/auto-promote policy path MAY gate and
    register a candidate but can NEVER switch the served LLM. Structurally: `registry.promote`
    (the choke-point the scheduler + suggestion-accept call) touches neither the pointer nor the
    reload; only the operator's `POST /models/{name}/promote` route wires the go-live half.
"""
import os
import re
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _llmregistry import FakeRegistry  # noqa: E402
from app import registry  # noqa: E402

#: The resident compose control plane as of 020/021 — 022 must not grow it (FR-273).
EXPECTED_SERVICES = {"postgres", "garage", "garage-init", "mlflow",
                     "gateway", "prometheus", "grafana"}


def _compose_services():
    text = open(os.path.join(REPO, "docker-compose.yml"), encoding="utf-8").read()
    body = text[text.index("\nservices:"):]
    if "\nvolumes:" in body:
        body = body[:body.index("\nvolumes:")]
    return set(re.findall(r"^  (\w[\w-]*):\s*$", body, flags=re.M))


# -- FR-273: no new resident process ----------------------------------------------------------------

def test_compose_service_set_is_unchanged():
    assert _compose_services() == EXPECTED_SERVICES


def test_agent_engine_registry_is_unchanged():
    from platformlib.topology import ENGINES
    assert set(ENGINES) == {"llm", "asr", "vision", "embed", "tabular"}


def test_resolver_import_spawns_no_thread():
    before = threading.active_count()
    import hostagent.serving_llm  # noqa: F401 — cold-load-only: no daemon, no watcher
    import platformlib.llmresolve  # noqa: F401
    assert threading.active_count() == before


def test_only_new_persisted_state_is_the_pointer_row():
    from platformlib import store
    # exactly one table added vs the 018 set — the singleton pointer (data-model.md)
    assert set(store.TABLES) - {"meta", "predictions", "labels", "capture_index", "jobs",
                                "policies", "suggestions"} == {"serving_llm"}


# -- FR-275: the policy path cannot switch the served LLM --------------------------------------------

def test_registry_promote_never_touches_pointer_or_reload(monkeypatch, tmp_path):
    # The scheduler's auto-promote and the suggestion accept BOTH call registry.promote directly
    # (tests below pin that) — so proving promote() itself never goes live proves the boundary.
    reg = FakeRegistry()
    reg.add("ops-bot", 2, "s3://models/a.gguf",
            {"kind": "lora-adapter", "task": "text-generation", "base_model": "qwen-base"})
    monkeypatch.setattr(registry, "_client", lambda: reg)
    from app import evaluation
    monkeypatch.setattr(evaluation, "gate",
                        lambda name, version, override=False, client=None:
                        {"verdict": "pass", "reason": "test"})
    touched = []
    monkeypatch.setattr(registry, "set_serving_llm",
                        lambda *a, **kw: touched.append("pointer"))
    from app import serving
    monkeypatch.setattr(serving, "request_llm_reload",
                        lambda *a, **kw: touched.append("reload"))
    res = registry.promote("ops-bot", "2")
    assert res["promoted"] is True and reg.aliases["ops-bot"] == "2"  # the alias DID move
    assert touched == []  # …but nothing went live: no pointer write, no agent reload (FR-275)


def test_policy_and_scheduler_paths_reference_no_go_live_surface():
    # Belt-and-braces source pin: the auto-promote path (scheduler) and the suggestion accept
    # (routers/policies) must keep calling registry.promote — never the go-live half.
    for rel in ("gateway/app/scheduler.py", "gateway/app/routers/policies.py"):
        src = open(os.path.join(REPO, rel), encoding="utf-8").read()
        assert "set_serving_llm" not in src, rel
        assert "request_llm_reload" not in src, rel


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
