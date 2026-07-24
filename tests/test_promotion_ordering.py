"""024 US2 (T573) — the web-free go-live ordering (`gateway/app/promotion.go_live`).

Drives the extracted use-case over FAKE `registry`/`activation` seams — no FastAPI, no live stack — and
pins the FR-265 ordering + the exact outcome→status→metric mapping the router replays
(contracts/preservation.md §C2 / data-model.md §Router mapping):

  - an unresolvable adapter is REFUSED before `registry.promote` is ever called (alias never moves);
  - a pre-alias conflict is CONFLICT before the alias moves (`conflict` only);
  - the post-promote TOCTOU conflict emits `["ok", "conflict"]`, 409, alias LEFT moved (a preserved
    existing behavior — invariant 4);
  - the prior serving pointer is captured BEFORE the durable activation overwrites it;
  - each outcome maps to the right HTTP status + ordered `REGISTRY_OPS` labels;

and asserts `go_live` has exactly one caller — the operator route (single-live-switch, SC-170).
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from gateway.app import promotion  # noqa: E402
from gateway.app.promotion import GoLiveOutcome, go_live  # noqa: E402


class FakeRegistry:
    class RegistryError(Exception):
        pass

    def __init__(self, *, versions=None, list_raises=False, target=None, target_raises=False,
                 promote_result=None, promote_raises=False, serving=None):
        self._versions = versions if versions is not None else [{"version": "1"}]
        self._list_raises = list_raises
        self._target = target
        self._target_raises = target_raises
        self._promote_result = promote_result if promote_result is not None else \
            {"promoted": True, "verdict": "pass"}
        self._promote_raises = promote_raises
        self._serving = serving
        self.calls = []

    def list_versions(self, name):
        self.calls.append("list_versions")
        if self._list_raises:
            raise self.RegistryError("list boom")
        return self._versions

    def llm_target_info(self, name, version):
        self.calls.append("llm_target_info")
        if self._target_raises:
            raise self.RegistryError("target boom")
        return self._target

    def promote(self, name, version, override=False):
        self.calls.append("promote")
        if self._promote_raises:
            raise self.RegistryError("promote boom")
        return dict(self._promote_result)

    def get_serving_llm(self):
        self.calls.append("get_serving_llm")
        return self._serving


class FakeActivation:
    class ActivationError(Exception):
        pass

    def __init__(self, *, conflict_precheck=False, activate_result=None, activate_raises=False):
        self._conflict_precheck = conflict_precheck
        self._activate_result = activate_result if activate_result is not None else \
            {"active": "m", "kind": "base", "base": None}
        self._activate_raises = activate_raises
        self.calls = []

    def service(self):
        return self

    def assert_no_conflict(self, name, version):
        self.calls.append("assert_no_conflict")
        if self._conflict_precheck:
            raise self.ActivationError("another activation is in progress")

    def activate(self, *, name, version, prior, preempt, kind, base):
        self.calls.append(("activate", prior))
        if self._activate_raises:
            raise self.ActivationError("another activation is in progress")
        return dict(self._activate_result)


_LLM = {"kind": "base", "base": None}  # a text-generation target (no adapter error)


def _run(reg, act, *, version="1", override=False, preempt=False):
    return go_live("m", version, override=override, preempt=preempt, registry=reg, activation=act)


# --- pre-checks: fail-loud to 502 with NO metric, before anything moves ---------------------------

def test_missing_version_is_not_found_404_no_metric_no_promote():
    reg = FakeRegistry(versions=[{"version": "1"}])
    r = _run(reg, FakeActivation(), version="2")
    assert r.outcome is GoLiveOutcome.NOT_FOUND and r.http_status == 404
    assert r.metric_statuses == ()                 # no REGISTRY_OPS increment
    assert "promote" not in reg.calls              # the alias never moved


def test_list_versions_registry_error_is_502_no_metric():
    r = _run(FakeRegistry(list_raises=True), FakeActivation())
    assert r.outcome is GoLiveOutcome.ERROR and r.http_status == 502 and r.metric_statuses == ()


def test_llm_target_registry_error_is_502_no_metric():
    r = _run(FakeRegistry(target_raises=True), FakeActivation())
    assert r.outcome is GoLiveOutcome.ERROR and r.http_status == 502 and r.metric_statuses == ()


# --- refuse / conflict BEFORE the alias moves (FR-265) --------------------------------------------

def test_unresolvable_adapter_refused_before_promote():
    reg = FakeRegistry(target={"error": "base 'x' does not resolve"})
    r = _run(reg, FakeActivation())
    assert r.outcome is GoLiveOutcome.REFUSED and r.http_status == 409
    assert r.metric_statuses == ("refused",)
    assert "promote" not in reg.calls              # refused HERE — the alias never moves


def test_pre_alias_conflict_refused_before_promote():
    reg = FakeRegistry(target=_LLM)
    r = _run(reg, FakeActivation(conflict_precheck=True))
    assert r.outcome is GoLiveOutcome.CONFLICT and r.http_status == 409
    assert r.metric_statuses == ("conflict",)
    assert "promote" not in reg.calls              # conflict caught before the alias moves


# --- the gated promote: error / blocked / promoted ------------------------------------------------

def test_promote_time_registry_error_emits_error_502():
    reg = FakeRegistry(target=_LLM, promote_raises=True)
    r = _run(reg, FakeActivation())
    assert r.outcome is GoLiveOutcome.ERROR and r.http_status == 502
    assert r.metric_statuses == ("error",)         # unlike the pre-checks, this DOES emit


def test_gate_block_is_blocked_200_promoted_false():
    reg = FakeRegistry(target=_LLM, promote_result={"promoted": False, "verdict": "block"})
    r = _run(reg, FakeActivation())
    assert r.outcome is GoLiveOutcome.BLOCKED and r.http_status == 200
    assert r.metric_statuses == ("blocked",)
    assert r.body["promoted"] is False


def test_non_llm_promote_is_ok_no_activation():
    reg = FakeRegistry(target=None, promote_result={"promoted": True, "verdict": "pass"})
    act = FakeActivation()
    r = _run(reg, act)
    assert r.outcome is GoLiveOutcome.PROMOTED and r.http_status == 200
    assert r.metric_statuses == ("ok",)
    assert act.calls == []                          # no activation for a non-LLM target


# --- text-generation go-live: prior captured before overwrite, then activate ----------------------

def test_llm_promote_captures_prior_before_activate_then_ok():
    reg = FakeRegistry(target=_LLM, serving={"model_name": "prev"})
    act = FakeActivation(activate_result={"active": "m", "kind": "base", "base": None})
    r = _run(reg, act)
    assert r.outcome is GoLiveOutcome.PROMOTED and r.metric_statuses == ("ok",)
    # prior pointer captured AFTER the alias move (promote) but BEFORE activate overwrites it (FR-265),
    # and activate receives that exact captured snapshot.
    activate_call = next(c for c in act.calls if isinstance(c, tuple))
    assert activate_call[0] == "activate" and activate_call[1] == {"model_name": "prev"}
    assert reg.calls.index("get_serving_llm") == reg.calls.index("promote") + 1
    assert r.body["serving_llm"] == {"active": "m", "kind": "base", "base": None}


def test_unresolvable_reload_emits_ok_then_unresolvable():
    reg = FakeRegistry(target=_LLM, serving={"model_name": "prev"})
    act = FakeActivation(activate_result={"active": "prev", "rolled_back": True})
    r = _run(reg, act)
    assert r.outcome is GoLiveOutcome.PROMOTED and r.http_status == 200
    assert r.metric_statuses == ("ok", "unresolvable")    # two emits, in order


def test_post_promote_toctou_conflict_emits_ok_then_conflict_alias_left_moved():
    reg = FakeRegistry(target=_LLM, serving={"model_name": "prev"})
    act = FakeActivation(activate_raises=True)
    r = _run(reg, act)
    assert r.outcome is GoLiveOutcome.CONFLICT and r.http_status == 409
    assert r.metric_statuses == ("ok", "conflict")        # `ok` already fired; alias LEFT moved
    assert "promote" in reg.calls                          # the alias DID move (not rolled back)


# --- single-live-switch invariant (SC-170 / FR-336) -----------------------------------------------

def test_go_live_has_exactly_one_caller():
    r = subprocess.run(
        ["grep", "-rn", "go_live(", os.path.join(REPO, "gateway"), "--include=*.py"],
        capture_output=True, text=True)
    callers = [ln for ln in r.stdout.splitlines() if "def go_live(" not in ln]
    files = {ln.split(":", 1)[0] for ln in callers}
    assert files == {os.path.join(REPO, "gateway", "app", "routers", "models.py")}, \
        f"go_live must have exactly one caller (the operator route); found {sorted(files)}"


def test_promotion_module_imports_no_web_framework():
    import inspect
    src = inspect.getsource(promotion)
    for driver in ("fastapi", "httpx"):
        assert f"import {driver}" not in src and f"from {driver}" not in src, \
            f"promotion must stay web-free — it imports {driver}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
