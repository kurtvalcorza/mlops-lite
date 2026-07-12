"""015 US3 — the gateway /evaluate guard (T296, FR-143, SC-091).

`evaluate_guarded` must never silently score the resident model for a *different* requested version (the
SC-068 mislabel). Offline, with the isolated 011 harness + fake client: a non-`@serving` version with no
logged metric is refused (`EvalGuardError`); a version that was scored at registration returns its logged
metric with **no** model load; the `@serving` version is still scored via the serving path.
"""
from _eval import FakeClient, FakeMV, load_evaluation

m = load_evaluation()
VIS = "image-classification"


def _ev_tags(metric="accuracy", value=0.8, direction=m.HIGHER, modality=VIS):
    return {"task": modality, m.TAG_METRIC: metric, m.TAG_VALUE: str(value),
            m.TAG_DIRECTION: direction, m.TAG_MODALITY: modality,
            m.TAG_BENCHMARK: "vision/shapes_smoke.jsonl", m.TAG_BENCHMARK_HASH: "h0"}


class NeverScores:
    def __call__(self, *a, **k):
        raise AssertionError("the guard scored a model — it must read the logged metric / refuse")


def test_unscored_non_serving_version_is_refused(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    client = FakeClient({
        ("clf", "1"): FakeMV(_ev_tags(), run_id="r1"),     # @serving, scored
        ("clf", "2"): FakeMV({"task": VIS}, run_id="r2"),  # NOT serving, never scored
    })
    try:
        m.evaluate_guarded("clf", "2", predict_fn=NeverScores(), client=client)
    except m.EvalGuardError as e:
        assert "no logged eval metric" in str(e)
    else:
        raise AssertionError("expected EvalGuardError for a non-@serving unscored version (SC-091)")


def test_eval_guard_error_is_an_eval_error(monkeypatch):
    """The guard error subclasses EvalError so existing handlers still catch it (router maps it to 409)."""
    assert issubclass(m.EvalGuardError, m.EvalError)


def test_scored_version_returns_logged_metric_without_loading(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    client = FakeClient({
        ("clf", "1"): FakeMV(_ev_tags(), run_id="r1"),
        ("clf", "2"): FakeMV(_ev_tags(value=0.42), run_id="r2"),  # not serving, but scored at registration
    })
    res = m.evaluate_guarded("clf", "2", predict_fn=NeverScores(), client=client)
    assert res["value"] == 0.42 and res["metric"] == "accuracy" and res["source"] == "logged"
    assert res["version"] == "2"


def test_serving_version_is_scored_via_the_serving_path(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    client = FakeClient({("clf", "1"): FakeMV({"task": VIS}, run_id="r1")})
    truth = m.load_benchmark(VIS).rows[0]["label"]

    # the requested version IS @serving → scoring the resident model is correct; inject the predictor.
    def predict(rows, modality, version):
        assert version == "1"
        return [truth] * len(rows)

    res = m.evaluate_guarded("clf", "1", predict_fn=predict, client=client)
    assert res["version"] == "1" and res["metric"] == "accuracy"
    assert "source" not in res  # freshly scored, not a logged read


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
