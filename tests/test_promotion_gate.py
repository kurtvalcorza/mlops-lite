"""011 US2 — gated promotion (T213/T214 / SC-065/SC-066).

Offline coverage of the gate's decision logic (compute_verdict) across every acceptance scenario and
both metric directions, plus gate() over a fake client, plus a live wiring leg that confirms the
single `registry.promote` choke-point returns a verdict for EVERY promotion (no ungated back-door).
"""
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers
from _eval import FakeClient, FakeMV, load_evaluation

m = load_evaluation()


def _ev(value, *, metric="accuracy", direction=None, modality="image-classification", version="1"):
    direction = direction or (m.LOWER if metric in ("wer", "perplexity") else m.HIGHER)
    return {"version": version, "metric": metric, "value": value, "direction": direction,
            "modality": modality}


# --- compute_verdict: the acceptance matrix -------------------------------------------------------

def test_regression_blocks_by_default_overrides_and_warns():
    cand, inc = _ev(0.80), _ev(0.90, version="2")  # higher-better → candidate regressed
    assert m.compute_verdict(cand, inc)["verdict"] == "blocked"            # AS-1: hard-gate refuses
    over = m.compute_verdict(cand, inc, override=True)                     # AS-2: override moves, flagged
    assert over["verdict"] == "warn" and over["flagged"] is True
    warn = m.compute_verdict(cand, inc, mode="warn")                       # AS-2: warn mode moves, flagged
    assert warn["verdict"] == "warn" and warn["flagged"] is True


def test_non_regressing_candidate_passes_in_all_modes():
    cand, inc = _ev(0.95), _ev(0.90, version="2")  # improvement
    for kwargs in ({}, {"mode": "warn"}, {"override": True}):
        assert m.compute_verdict(cand, inc, **kwargs)["verdict"] == "pass"  # AS-3


def test_within_tolerance_is_not_a_regression():
    cand, inc = _ev(0.895), _ev(0.90, version="2")  # 0.5% worse, tolerance 1% → within noise
    assert m.compute_verdict(cand, inc, tolerance=0.01)["verdict"] == "pass"
    assert m.compute_verdict(cand, inc, tolerance=0.0)["verdict"] == "blocked"  # zero tolerance bites


def test_lower_is_better_direction_is_honoured():
    # WER/perplexity: a HIGHER value is the regression. Get the sign right or the gate inverts.
    worse, better = _ev(0.30, metric="wer", modality="asr"), _ev(0.20, metric="wer", modality="asr", version="2")
    assert m.compute_verdict(worse, better)["verdict"] == "blocked"
    assert m.compute_verdict(better, worse)["verdict"] == "pass"


def test_no_incumbent_passes():
    assert m.compute_verdict(_ev(0.5), None)["verdict"] == "pass"          # edge: first promotion


def test_missing_metric_policy_is_explicit_never_silent():
    inc = _ev(0.9, version="2")
    assert m.compute_verdict(None, inc, missing_policy="warn")["verdict"] == "warn"   # AS-4 (bootstrap)
    blocked = m.compute_verdict(None, inc, missing_policy="block")
    assert blocked["verdict"] == "blocked" and blocked["flagged"] is True
    # an explicit override still gets the operator past a missing metric.
    assert m.compute_verdict(None, inc, missing_policy="block", override=True)["verdict"] == "warn"


def test_cross_modality_mismatch_refuses_to_judge():
    vis, llm = _ev(0.9, metric="accuracy"), _ev(0.9, metric="task_accuracy", modality="text-generation", version="2")
    assert m.compute_verdict(vis, llm)["verdict"] == "blocked"            # like-for-like only (FR-103)
    assert m.compute_verdict(vis, llm, override=True)["verdict"] == "warn"


def test_verdict_carries_candidate_incumbent_delta_tolerance_mode():
    v = m.compute_verdict(_ev(0.80), _ev(0.90, version="2"))
    for key in ("verdict", "reason", "candidate", "incumbent", "delta", "tolerance", "mode"):
        assert key in v
    assert v["candidate"]["value"] == 0.80 and v["incumbent"]["value"] == 0.90
    assert v["delta"] == -0.10  # signed candidate - incumbent


# --- gate() over a fake client --------------------------------------------------------------------

def _tags(ev):
    return {m.TAG_METRIC: ev["metric"], m.TAG_VALUE: repr(ev["value"]), m.TAG_DIRECTION: ev["direction"],
            m.TAG_MODALITY: ev["modality"], m.TAG_BENCHMARK: "b", m.TAG_BENCHMARK_HASH: "h"}


def test_gate_reads_incumbent_from_serving_alias(monkeypatch):
    client = FakeClient({
        ("clf", "1"): FakeMV(_tags(_ev(0.90))),   # incumbent @serving
        ("clf", "2"): FakeMV(_tags(_ev(0.80))),   # regressing candidate
    })
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    assert m.gate("clf", "2", client=client)["verdict"] == "blocked"
    assert m.gate("clf", "2", client=client, override=True)["verdict"] == "warn"


def test_gate_repromoting_serving_version_has_no_incumbent(monkeypatch):
    client = FakeClient({("clf", "1"): FakeMV(_tags(_ev(0.90)))})
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    # re-promoting the current serving version: nothing to regress against → pass.
    assert m.gate("clf", "1", client=client)["verdict"] == "pass"


def test_gate_exposes_candidate_version_even_without_a_metric(monkeypatch):
    # an unevaluated candidate against an evaluated incumbent, missing-policy=block: the verdict still
    # carries the candidate version so the UI can offer an override on the block (no null candidate).
    client = FakeClient({
        ("clf", "1"): FakeMV(_tags(_ev(0.90))),   # incumbent has a metric
        ("clf", "2"): FakeMV({"task": "image-classification"}),  # candidate has none
    })
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    v = m.gate("clf", "2", client=client, missing_policy="block")
    assert v["verdict"] == "blocked" and v["candidate"] == {"version": "2"}


# --- live wiring leg: every promotion returns a verdict (no ungated path) -------------------------

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
LLM_TASK = "text-generation"


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> int:
    """Wiring check (no GPU): register two task-tagged versions with NO eval metric and promote each.
    The gate must run on every promotion (single choke-point) and return a verdict — first promotion
    'no incumbent', second under the missing-metric policy — never an ungated alias move (SC-066)."""
    name = os.getenv("GATE_TEST_MODEL", "gate-smoke-llm")
    s1, v1 = _req("POST", "/models", {"name": name, "source": "s3://m/v1", "tags": {"task": LLM_TASK}})
    s2, v2 = _req("POST", "/models", {"name": name, "source": "s3://m/v2", "tags": {"task": LLM_TASK}})
    if s1 != 201 or s2 != 201:
        print(f"[FAIL] register -> {s1} {v1} / {s2} {v2}")
        return 1
    a, b = v1["version"], v2["version"]

    sp, pr = _req("POST", f"/models/{name}/promote", {"version": a})
    if sp != 200 or "verdict" not in pr or pr.get("promoted") is not True:
        print(f"[FAIL] first promote (expect pass+verdict) -> {sp} {pr}")
        return 1
    if (pr["verdict"]["incumbent"] is not None) or pr["verdict"]["verdict"] != "pass":
        print(f"[FAIL] first promote should be 'no incumbent' pass -> {pr['verdict']}")
        return 1
    print(f"[OK] first promote of v{a} returned a verdict (pass, no incumbent)")

    sp2, pr2 = _req("POST", f"/models/{name}/promote", {"version": b})
    if sp2 != 200 or "verdict" not in pr2:
        print(f"[FAIL] second promote missing verdict -> {sp2} {pr2}")
        return 1
    # default missing-metric policy is warn → the alias moves but the event is flagged.
    if pr2["verdict"]["verdict"] != "warn" or pr2.get("promoted") is not True:
        print(f"[FAIL] second promote (expect warn+moved under missing-metric policy) -> {pr2['verdict']}")
        return 1
    print(f"[OK] second promote of v{b} gated + flagged (missing-metric warn), verdict returned by API")

    _, model = _req("GET", f"/models/{name}")
    if (model.get("serving") or {}).get("version") != str(b):
        print(f"[FAIL] serving alias did not land on v{b}")
        return 1
    print("\nT214 PASS — every promotion runs through the gate and returns a verdict (no ungated path)")
    return 0


def test_promotion_gate_wiring(require_gateway, require_key):
    """Live leg: skip without a keyed stack (the gate wiring needs the gateway + MLflow)."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
