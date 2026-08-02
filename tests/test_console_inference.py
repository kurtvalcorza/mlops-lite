"""027 T736/T741/T749 — evaluations, payload safety, and the endpoint status rule.

Three surfaces, three rules that are each one careless line away from being violated:

  * an evaluation must not coerce modality-native metrics into a common score,
  * a payload must not be **sent** unless it was explicitly asked for — hidden-by-default has to be
    structural, not a styling choice,
  * an endpoint must not be called `healthy` on the strength of a desired pointer, and must not be
    called `failed` when it is simply not loaded.

All offline. Every rule here is a pure function over plain dicts, which is the point: these are
claims about what the console is allowed to say, and they should not need a platform to state.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from tests import _gwimport  # noqa: E402

with _gwimport.isolated_metrics():
    from gateway.app.console import endpoints as endpoints_mod  # noqa: E402
    from gateway.app.console import evaluations as evaluations_mod  # noqa: E402
    from gateway.app.console import predictions as predictions_mod  # noqa: E402
    from gateway.app.console import traces as traces_mod  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _isolate_gateway_metrics():
    yield from _gwimport.isolate_module_metrics()


# -- T736: evaluations -----------------------------------------------------------------------------

def test_modality_native_metrics_are_not_coerced_into_a_common_score():
    """FR-399. A WER, an accuracy and a perplexity are not comparable; a single 'score' column would
    produce a leaderboard ranking an ASR model against a classifier."""
    wer = evaluations_mod.evaluation_result(
        name="asr", version="1",
        evaluation={"metric": "wer", "value": 0.18, "direction": "lower", "modality": "asr"})
    accuracy = evaluations_mod.evaluation_result(
        name="vision", version="1",
        evaluation={"metric": "accuracy", "value": 0.91, "direction": "higher",
                    "modality": "image-classification"})

    assert wer["metrics"][0]["name"] == "wer"
    assert accuracy["metrics"][0]["name"] == "accuracy"
    assert "score" not in wer and "score" not in accuracy
    for result in (wer, accuracy):
        assert set(result["metrics"][0]) == {"name", "value", "direction"}


def test_every_metric_carries_its_own_direction():
    """Without it, '0.31' cannot be read as good or bad, and a surface assuming higher-better would
    rank every WER backwards."""
    result = evaluations_mod.evaluation_result(
        name="asr", version="1",
        evaluation={"metric": "wer", "value": 0.18, "direction": "lower"})
    assert result["metrics"][0]["direction"] == "lower-better"


def test_an_unevaluated_version_reads_not_evaluated_rather_than_an_error():
    """FR-402: the platform's documented refusal to score it, not a failure."""
    result = evaluations_mod.evaluation_result(name="m", version="1", evaluation=None)
    assert result["gate"]["outcome"] == "not-evaluated"
    assert result["metrics"] == []


def test_a_scored_version_with_no_gate_run_is_incomplete_not_not_evaluated():
    """Different situations: 'nothing to gate' and 'gated but no verdict recorded'. Collapsing them
    hides which one the operator is looking at."""
    result = evaluations_mod.evaluation_result(
        name="m", version="1", evaluation={"metric": "accuracy", "value": 0.9})
    assert result["gate"]["outcome"] == "incomplete"


def test_a_gate_failure_carries_the_rule_the_observed_value_and_the_incumbent():
    """SC-191: all of it reachable without leaving the view. A verdict without its evidence sends
    the operator to the tracking UI to reconstruct why, which is the round trip this removes."""
    verdict = {
        "verdict": "blocked", "reason": "candidate regresses beyond tolerance",
        "mode": "hard", "tolerance": 0.02, "override": False, "delta": -0.05,
        "candidate": {"version": "4", "metric": "accuracy", "value": 0.86, "direction": "higher",
                      "modality": "image-classification"},
        "incumbent": {"version": "3", "metric": "accuracy", "value": 0.91, "direction": "higher",
                      "modality": "image-classification"},
    }
    gate = evaluations_mod.gate_view(evaluation=verdict["candidate"], verdict=verdict)
    assert gate["outcome"] == "failed"
    assert gate["failedRule"]["metric"] == "accuracy" and gate["failedRule"]["operator"] == "gte"
    assert gate["observedValue"] == 0.86
    assert gate["comparedAgainst"] == {"version": "3", "value": 0.91}
    # The threshold in the metric's own units, so the operator reads the reason rather than an
    # approximation of it: 0.91 less the 2% tolerance.
    assert gate["failedRule"]["threshold"] == pytest.approx(0.8918)


def test_a_lower_better_metric_inverts_the_rule_operator_and_threshold():
    verdict = {
        "verdict": "blocked", "tolerance": 0.02, "override": False,
        "candidate": {"version": "2", "metric": "wer", "value": 0.25, "direction": "lower"},
        "incumbent": {"version": "1", "metric": "wer", "value": 0.18, "direction": "lower"},
    }
    gate = evaluations_mod.gate_view(evaluation=verdict["candidate"], verdict=verdict)
    assert gate["failedRule"]["operator"] == "lte"
    assert gate["failedRule"]["threshold"] == pytest.approx(0.1836), "the ceiling, not the floor"


def test_an_override_travels_with_its_reason():
    """FR-401: an override with no reason is indistinguishable from a gate never enforced."""
    gate = evaluations_mod.gate_view(
        evaluation={"metric": "accuracy", "value": 0.8},
        verdict={"verdict": "warn", "override": True, "override_reason": "known-good regression",
                 "candidate": {"metric": "accuracy", "value": 0.8}, "incumbent": {}})
    assert gate["override"]["applied"] is True
    assert gate["override"]["reason"] == "known-good regression"


# -- T738: drift ------------------------------------------------------------------------------------

def test_drift_thresholds_ship_inline_so_the_interface_never_restates_them():
    """FR-405: tuning the convention in configuration must not leave the console quietly
    disagreeing with the backend about what counts as drift."""
    report = evaluations_mod.drift_report(
        {"model_name": "churn", "features": [{"name": "age", "psi": 0.31}]})
    assert report["thresholds"] == {"warning": 0.10, "significant": 0.25, "configurable": True}


def test_drift_features_are_banded_once_rather_than_by_each_surface():
    report = evaluations_mod.drift_report({"features": [
        {"name": "a", "psi": 0.02}, {"name": "b", "psi": 0.15}, {"name": "c", "psi": 0.40}]})
    assert [f["state"] for f in report["features"]] == ["stable", "warning", "significant"]


def test_an_empty_drift_report_has_a_null_max_rather_than_zero():
    """A max of zero reads as 'no drift', which is a claim an empty feature list cannot support."""
    report = evaluations_mod.drift_report({"features": []})
    assert report["maxStatistic"] is None and report["maxStatistic"] != 0


def test_the_drift_limitations_are_stated_on_the_surface():
    """FR-406: all four, and they are a static property of the surface rather than per-report data —
    attaching them per report would imply some report is exempt."""
    text = " ".join(evaluations_mod.DRIFT_LIMITATIONS).lower()
    assert "does not prove" in text
    assert "causality" in text
    assert "baseline" in text
    assert "binning" in text


def test_the_comparison_keeps_its_six_dimensions_separate():
    """FR-403: a challenger that wins on quality and loses on latency is a trade-off the operator
    has to make, and a combined score would make it for them silently."""
    result = evaluations_mod.comparison(
        challenger={"name": "m", "version": "2"}, champion={"name": "m", "version": "1"})
    for dimension in evaluations_mod.COMPARISON_DIMENSIONS:
        assert dimension in result
    assert "overall" not in result and "score" not in result


def test_unmeasured_comparison_dimensions_are_null_rather_than_zero():
    """This platform does not record per-version latency or resource history. A column of zeros
    would be worse than an empty one: a zero invites comparison."""
    result = evaluations_mod.comparison(challenger={"name": "m", "version": "2"}, champion=None)
    assert result["latency"] == {"challenger": None, "champion": None}
    assert result["resources"]["challenger"] is None


# -- T741: payload safety ---------------------------------------------------------------------------

def test_a_payload_preview_omits_the_preview_key_entirely_when_not_revealed():
    """Not `None`, not `''`, not a placeholder — ABSENT. A key that is present but empty is one
    `??` away from being filled in by a well-meaning change; an absent key makes the omission
    visible in the payload itself (FR-408)."""
    preview = predictions_mod.payload_preview(available=True, total_bytes=1024)
    assert "preview" not in preview
    assert preview["revealed"] is False and preview["available"] is True


def test_a_revealed_payload_carries_its_content_and_states_the_true_size():
    preview = predictions_mod.payload_preview(
        available=True, revealed=True, content="hello", total_bytes=5)
    assert preview["preview"] == "hello" and preview["revealed"] is True
    assert preview["truncated"] is False and preview["totalBytes"] == 5


def test_a_large_payload_truncates_and_still_states_the_true_size():
    """spec Edge Cases: a multi-megabyte transcript rendered into a browser panel is a page that
    stops responding, and the operator wanted the shape, not the bytes."""
    body = "x" * (predictions_mod.PREVIEW_LIMIT_BYTES * 3)
    preview = predictions_mod.payload_preview(
        available=True, revealed=True, content=body, total_bytes=len(body))
    assert preview["truncated"] is True
    assert len(preview["preview"]) <= predictions_mod.PREVIEW_LIMIT_BYTES
    assert preview["totalBytes"] == len(body), "the TRUE size, not the truncated one"


def test_a_prediction_record_carries_no_payload_field_at_all():
    """The record itself never has anywhere for content to land."""
    record = predictions_mod.prediction_record(
        {"prediction_id": "p-1", "modality": "text-generation", "payload_ref": "s3://b/k"})
    assert "payload" not in record and "preview" not in record
    assert record["captureState"] == "captured", "the capture is reported; its content is not"


def test_the_review_queue_states_which_signal_put_each_item_there():
    """FR-411. A queue that ranks without saying why is one an operator has to take on faith, and
    the first surprising ordering costs it all its credibility."""
    items = predictions_mod.review_queue([
        {"prediction_id": "p-1", "label": None, "captured_at": 100},
        {"prediction_id": "p-2", "policy_result": "blocked", "label": None, "captured_at": 90},
    ])
    assert items[0]["predictionId"] == "p-2", "a policy-flagged item outranks a merely unlabeled one"
    assert items[0]["reason"] == "policy-flagged"
    assert "missing-label" in items[1]["signals"]


def test_an_item_with_no_signal_is_not_in_the_queue():
    assert predictions_mod.review_queue([
        {"prediction_id": "p-1", "label": "cat", "policy_result": "ok"}]) == []


# -- T745: trace normalization -----------------------------------------------------------------------

def _span(span_id, parent, name, start_ns, end_ns, **attrs):
    return {"span_id": span_id, "parent_id": parent, "name": name, "start_time_ns": start_ns,
            "end_time_ns": end_ns, "attributes": attrs, "events": [], "status": "OK"}


def test_a_trace_normalizes_to_a_generic_span_tree_with_no_token_fields():
    """FR-413. This platform serves five modalities: an image classification has no tokens, an
    embedding call has no completion. A waterfall assuming them renders three of five as broken."""
    detail = traces_mod.normalize({
        "info": {"trace_id": "t-1", "tags": {"prediction_id": "p-1"}},
        "spans": [_span("s1", None, "predict", 1_000_000_000, 1_500_000_000, prompt_tokens=12)],
    })
    span = detail["spans"][0]
    assert set(span) == {"spanId", "parentSpanId", "name", "startMs", "durationMs", "attributes",
                         "events", "status", "error"}
    # Token-shaped data survives — under the name its producer gave it, in the opaque bag.
    assert span["attributes"]["prompt_tokens"] == 12


def test_span_starts_are_relative_to_the_trace_so_a_waterfall_begins_at_zero():
    detail = traces_mod.normalize({"info": {"trace_id": "t"}, "spans": [
        _span("s1", None, "a", 5_000_000_000, 5_500_000_000),
        _span("s2", "s1", "b", 5_200_000_000, 5_400_000_000)]})
    assert detail["spans"][0]["startMs"] == 0.0
    assert detail["spans"][1]["startMs"] == pytest.approx(200.0)
    assert detail["totalDurationMs"] == pytest.approx(500.0)


def test_a_cyclic_trace_renders_rather_than_hanging():
    """A malformed trace can contain a cycle. A rendering that hangs is worse than one that shows a
    span at the wrong indentation."""
    spans = [{"spanId": "a", "parentSpanId": "b", "name": "a"},
             {"spanId": "b", "parentSpanId": "a", "name": "b"}]
    depths = traces_mod.depth_of(spans)
    assert set(depths) == {"a", "b"}


# -- T749: the endpoint status rule ------------------------------------------------------------------

def test_healthy_requires_resident_confirmation():
    """FR-416: a desired-but-unloaded model called `healthy` reports an intention as an outcome."""
    pending = endpoints_mod.endpoint(
        modality="text-generation",
        desired={"modelName": "qwen", "version": "3"},
        engine={"engine_id": "llm", "state": "cold"})
    assert pending["status"] == "pending"

    healthy = endpoints_mod.endpoint(
        modality="text-generation",
        desired={"modelName": "qwen", "version": "3"},
        engine={"engine_id": "llm", "state": "ready", "model_identity": "qwen-v3",
                "registry_version": "3", "residency_state": "resident"})
    assert healthy["status"] == "healthy"


def test_a_gpu_modality_blocked_by_a_job_is_stopped_not_failed():
    """FR-417 and the reason it exists: on-demand loading is the design (Principle II). The model is
    not broken, it is not loaded, and calling that a failure sends an operator to debug a working
    system."""
    result = endpoints_mod.endpoint(
        modality="text-generation",
        desired={"modelName": "qwen", "version": "3"},
        engine={"engine_id": "llm", "state": "cold"},
        job_holds_gpu=True)
    assert result["status"] == "stopped"
    assert result["status"] != "failed"


def test_a_cpu_modality_is_healthy_whenever_its_child_answers():
    """Off-lease: there is no residency question to ask."""
    result = endpoints_mod.endpoint(
        modality="embedding",
        desired={"modelName": "minilm", "version": "1"},
        engine={"engine_id": "embed", "state": "ready"})
    assert result["status"] == "healthy"


def test_an_unreachable_agent_makes_the_status_unknown_never_healthy_or_failed():
    """We cannot see residency, and residency is what `healthy` requires."""
    result = endpoints_mod.endpoint(
        modality="text-generation",
        desired={"modelName": "qwen", "version": "3"},
        agent_reachable=False)
    assert result["status"] == "unknown"
    assert result["status"] not in ("healthy", "failed")


def test_the_resident_identity_is_the_agent_report_never_the_desired_pointer():
    """The two legitimately diverge during an in-flight activation — exactly when an operator is
    looking — and sourcing this from the pointer would manufacture agreement that does not exist."""
    result = endpoints_mod.endpoint(
        modality="text-generation",
        desired={"modelName": "qwen", "version": "4"},
        engine={"engine_id": "llm", "state": "ready", "model_identity": "qwen-v3",
                "registry_version": "3", "residency_state": "resident"})
    assert result["resident"]["modelIdentity"] == "qwen-v3"
    assert result["desired"]["version"] == "4"
    assert result["status"] == "degraded", "serving, but not the thing the operator believes"
    assert result["conflict"]["conflict"] is True


def test_an_endpoint_with_nothing_desired_and_nothing_resident_is_unconfigured():
    assert endpoints_mod.endpoint(modality="asr")["status"] == "unconfigured"


def test_every_produced_status_is_in_the_declared_vocabulary():
    """FR-415: the complete vocabulary, and nothing outside it."""
    cases = [
        {},
        {"desired": {"modelName": "m", "version": "1"}},
        {"desired": {"modelName": "m"}, "engine": {"state": "loading"}},
        {"desired": {"modelName": "m"}, "engine": {"state": "wedged"}},
        {"desired": {"modelName": "m"}, "engine": {"residency_state": "draining"}},
        {"desired": {"modelName": "m"}, "agent_reachable": False},
    ]
    for case in cases:
        status = endpoints_mod.endpoint(modality="text-generation", **case)["status"]
        assert status in endpoints_mod.STATUSES, status
