"""018 T358 — fold-in wiring regressions (offline).

Covers two review-round-2 findings that live outside the adapter itself:
  * P1: the native batch-inference flow's LLM serving URL must target the agent, not the retired
    :8090 llama daemon (the WSL trainer doesn't inherit the gateway's injected SERVING_URL).
  * P2: a legacy `SUPERVISE_DAEMONS=serving,...` override must map `serving` -> `agent`, or LLM
    serving is silently left unsupervised while the gateway points SERVING_URL at :8100.
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(mod_name, *relpath):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(REPO, *relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_batch_infer_llm_url_defaults_to_agent(monkeypatch):
    monkeypatch.delenv("SERVING_URL", raising=False)
    mod = _load("batch_infer_under_test", "training", "flows", "batch_infer.py")
    # native (WSL) default → the agent's /engines/llm, NOT the deleted :8090 llama daemon
    assert mod.SERVING_URL == "http://localhost:8100/engines/llm"


def test_supervise_maps_legacy_serving_and_training_selection_to_agent(monkeypatch):
    # 018 T362: `training` also folds into the agent, so a legacy `serving,training,ui` override
    # maps BOTH serving and training -> agent (deduped), leaving {agent, ui}.
    monkeypatch.setenv("SUPERVISE_DAEMONS", "serving,training,ui")
    mod = _load("supervise_under_test", "supervisor", "supervise.py")
    assert "serving" not in mod._SELECTED and "training" not in mod._SELECTED
    assert mod._SELECTED == ["agent", "ui"]


def test_supervise_dedups_when_both_serving_and_agent_listed(monkeypatch):
    monkeypatch.setenv("SUPERVISE_DAEMONS", "serving,agent,training")
    mod = _load("supervise_under_test2", "supervisor", "supervise.py")
    assert mod._SELECTED.count("agent") == 1
    assert mod._SELECTED == ["agent"]  # 018 T362: training also -> agent, all collapse to one entry


def test_supervise_default_set_includes_agent_not_serving(monkeypatch):
    monkeypatch.delenv("SUPERVISE_DAEMONS", raising=False)
    mod = _load("supervise_under_test3", "supervisor", "supervise.py")
    assert "agent" in mod._SELECTED and "serving" not in mod._SELECTED


def test_supervise_probes_the_public_readyz(monkeypatch):
    # 023 US2 (FR-283): /engines/llm/health is behind the internal key now — an unauthenticated
    # supervisor probe would read 401 as "dead" and restart-loop a healthy agent. The probe target
    # is the PUBLIC minimal /readyz (process liveness + readiness), which needs no key.
    monkeypatch.delenv("AGENT_HEALTH", raising=False)
    mod = _load("supervise_under_test4", "supervisor", "supervise.py")
    assert mod._ALL["agent"]["health_url"].endswith(":8100/readyz")


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
