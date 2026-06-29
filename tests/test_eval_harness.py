"""011 US1 — offline evaluation harness (T208 / SC-064).

Two layers, both runnable anywhere (no live stack):
  - the pure per-modality primary-metric functions + direction registry (vision/LLM committed, the
    ASR/embeddings/tabular stubs), and the benchmark fixtures (loadable + content-hashed);
  - evaluate() end-to-end against a fake MLflow client + an injected predict_fn — the primary metric
    is computed, logged to MLflow against the version (tags + run metric) with benchmark provenance,
    and **re-running yields the same score** (reproducible, SC-064).
"""
import sys

from _eval import FakeClient, FakeMV, load_evaluation

m = load_evaluation()


# --- primary metrics + direction ------------------------------------------------------------------

def test_committed_metric_directions():
    assert m.METRICS["image-classification"].direction == m.HIGHER  # vision top-1 accuracy
    assert m.METRICS["text-generation"].direction == m.HIGHER       # LLM task-accuracy
    assert m.PERPLEXITY.direction == m.LOWER                        # LLM fallback
    # guidance stubs
    assert m.METRICS["asr"].direction == m.LOWER                    # WER
    assert m.METRICS["embedding"].direction == m.HIGHER             # recall@k
    assert m.METRICS["tabular"].direction == m.HIGHER               # AUC


def test_accuracy_and_task_accuracy():
    assert m.accuracy(["a", "b", "c"], ["a", "x", "c"]) == 2 / 3
    # generative answers: exact (normalised) and reference-as-token-run both count.
    assert m.task_accuracy(["Paris.", "the answer is 7", "blue"], ["paris", "7", "green"]) == 2 / 3


def test_task_accuracy_is_token_level_not_raw_substring():
    # token-level guards against inflated scores: "17" must NOT count as containing "7", nor "blue"
    # as containing "lu" (raw substring would wrongly score both as hits).
    assert m.task_accuracy(["the total is 17"], ["7"]) == 0.0
    assert m.task_accuracy(["blue"], ["lu"]) == 0.0
    assert m.task_accuracy(["it is 7 today"], ["7"]) == 1.0  # genuine token run still counts


def test_wer_is_lower_better_and_correct():
    assert m.wer(["the cat sat"], ["the cat sat"]) == 0.0
    assert m.wer(["the cat sat"], ["the cat sat down"]) == 0.25  # one deletion / four ref words


def test_recall_at_k_and_auc():
    assert m.recall_at_k([[1, 2, 3], [9, 8, 7]], [3, 1], k=3) == 0.5
    assert m.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75


def test_perplexity_lower_better():
    assert round(m.perplexity([[0.0, 0.0]]), 6) == 1.0
    assert m.perplexity([[1.0]]) > m.perplexity([[0.1]])  # higher NLL → higher perplexity


def test_direction_for_recovers_known_metric_directions():
    # the gate must never silently assume higher-better: a direction is recoverable from the metric
    # name alone, so an eval tag missing TAG_DIRECTION still gates lower-better metrics correctly.
    assert m.direction_for("wer") == m.LOWER
    assert m.direction_for("perplexity") == m.LOWER
    assert m.direction_for("accuracy") == m.HIGHER
    assert m.direction_for("auc") == m.HIGHER
    assert m.direction_for("nonsense-metric") is None


def test_read_eval_recovers_lower_direction_when_tag_absent():
    # a WER eval tag written WITHOUT TAG_DIRECTION must still read back as lower-better (not the old
    # silent HIGHER default that would invert the gate).
    client = FakeClient({("asr", "1"): FakeMV({
        "task": "asr", m.TAG_METRIC: "wer", m.TAG_VALUE: "0.2",
        # TAG_DIRECTION intentionally omitted
    })})
    ev = m.read_eval(client, "asr", "1")
    assert ev["metric"] == "wer" and ev["direction"] == m.LOWER


def test_metric_for_resolves_default_and_fallback_and_rejects_unknown():
    assert m.metric_for("text-generation").name == "task_accuracy"
    assert m.metric_for("text-generation", "perplexity").name == "perplexity"
    try:
        m.metric_for("image-classification", "wer")
    except m.EvalError:
        pass
    else:
        raise AssertionError("expected EvalError for a cross-modality metric name")


# --- benchmark fixtures ---------------------------------------------------------------------------

def test_benchmarks_load_and_are_content_hashed():
    llm = m.load_benchmark("text-generation")
    vis = m.load_benchmark("image-classification")
    assert llm.rows and all("prompt" in r and "answer" in r for r in llm.rows)
    assert vis.rows and all("image_b64" in r and "label" in r for r in vis.rows)
    # provenance: a stable content digest, identical across reloads (reproducible attribution).
    assert llm.digest == m.load_benchmark("text-generation").digest
    assert llm.digest != vis.digest


# --- evaluate() end-to-end (fake client + injected predict_fn) ------------------------------------

def _perfect_predict(rows, modality, version):
    """A model that always answers correctly → metric should be a perfect score."""
    return [r.get("answer", r.get("label")) for r in rows]


def test_evaluate_computes_logs_and_is_reproducible():
    client = FakeClient({("toy-llm", "1"): FakeMV({"task": "text-generation"}, run_id="r1")})
    r1 = m.evaluate("toy-llm", "1", predict_fn=_perfect_predict, client=client)

    assert r1["metric"] == "task_accuracy" and r1["direction"] == m.HIGHER
    assert r1["value"] == 1.0 and r1["modality"] == "text-generation"
    assert r1["benchmark"] and r1["benchmark_hash"]

    # logged to MLflow against the version: tags the gate reads back + a run metric.
    tags = client.versions[("toy-llm", "1")].tags
    assert tags[m.TAG_METRIC] == "task_accuracy"
    assert float(tags[m.TAG_VALUE]) == 1.0
    assert tags[m.TAG_DIRECTION] == m.HIGHER
    assert tags[m.TAG_BENCHMARK] and tags[m.TAG_BENCHMARK_HASH] == r1["benchmark_hash"]
    assert ("r1", "eval_task_accuracy", 1.0) in client.logged_metrics

    # reproducible — same version + benchmark + predictions → identical score (SC-064).
    r2 = m.evaluate("toy-llm", "1", predict_fn=_perfect_predict, client=client)
    assert r2["value"] == r1["value"] and r2["benchmark_hash"] == r1["benchmark_hash"]


def test_evaluate_round_trips_through_read_eval():
    client = FakeClient({("toy-llm", "1"): FakeMV({"task": "text-generation"}, run_id="r1")})
    m.evaluate("toy-llm", "1", predict_fn=_perfect_predict, client=client)
    ev = m.read_eval(client, "toy-llm", "1")
    assert ev and ev["metric"] == "task_accuracy" and ev["value"] == 1.0
    assert m.read_eval(client, "toy-llm", "2") is None  # unknown version → None, not an error


def test_evaluate_rejects_untagged_version():
    client = FakeClient({("mystery", "1"): FakeMV({}, run_id="r1")})  # no task tag
    try:
        m.evaluate("mystery", "1", predict_fn=_perfect_predict, client=client)
    except m.EvalError:
        pass
    else:
        raise AssertionError("expected EvalError when the version has no task tag")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
