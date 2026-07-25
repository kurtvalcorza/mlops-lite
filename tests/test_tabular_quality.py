"""025 US2 (T605) — tabular prediction logging + identity contract (FR-353).

Tabular is a full quality participant now, so `/predict` must mint a **per-row prediction id**, return it
to the caller (the label endpoint takes a caller-supplied id and there is no prediction-list API), and
log a **version-scoped** row carrying the NUMERIC score — otherwise `quality.window()` has nothing to
join and `evaluation.auc` has nothing rankable.

Offline: the child + registry + quality store are fakes; the route is driven directly (no live stack).
"""
import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.routers import tabular as tab  # noqa: E402

CHILD_OK = {"model": "tab", "device": "cpu", "features": ["x1"],
            "predictions": [{"prediction": 1, "score": 0.87}, {"prediction": 0, "score": 0.12}]}


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.posted.append((url, json))
        return self._resp


def _wire(monkeypatch, resp, *, serving=("tab-model", "7")):
    """Point the route at a fake child + registry + quality. `pending` collects the fire-and-forget
    logging coroutine so the test can await it in the SAME loop — deterministic, no race."""
    logged, captured, spawned, pending = [], [], [], []
    client = FakeClient(resp)
    monkeypatch.setattr(tab.httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setattr(tab.registry, "resolve_serving_target",
                        lambda task, prefer=None: ({"name": serving[0], "version": serving[1]}
                                                   if serving else None))
    monkeypatch.setattr(tab.quality, "log_prediction",
                        lambda name, version, modality, input_ref, prediction, prediction_id=None:
                        logged.append(dict(name=name, version=version, modality=modality,
                                           prediction=prediction, pid=prediction_id)) or prediction_id)
    monkeypatch.setattr(tab.quality, "capture_input",
                        lambda pid, modality, payload: captured.append((pid, modality, payload)))
    def _spawn(coro, kind=None):
        spawned.append(kind)
        pending.append(coro)      # awaited by `_run` inside the same loop (never left dangling)

    monkeypatch.setattr(tab.background, "spawn", _spawn)
    return logged, captured, spawned, pending


def _run(rows, pending):
    """Call the route and drain its fire-and-forget logging in one event loop."""
    async def _go():
        out = await tab.predict(tab.PredictRequest(rows=rows))
        for coro in pending:
            await coro
        return out

    return asyncio.run(_go())


# --- the identity contract returned to the caller --------------------------------------------------

def test_per_row_prediction_ids_are_returned(monkeypatch):
    *_, pending = _wire(monkeypatch, FakeResp(CHILD_OK))
    out = _run([{"x1": 1.0}, {"x1": 2.0}], pending)
    ids = out["prediction_ids"]
    assert len(ids) == 2 and len(set(ids)) == 2            # one distinct id PER ROW
    # …and positionally alongside each prediction, so a caller can label row-by-row.
    assert [p["prediction_id"] for p in out["predictions"]] == ids
    # the child's own fields survive untouched (byte-compatible superset)
    assert out["predictions"][0]["prediction"] == 1 and out["predictions"][0]["score"] == 0.87
    assert out["model"] == "tab" and out["features"] == ["x1"]


def test_empty_predictions_yield_no_ids_and_no_logging(monkeypatch):
    logged, captured, spawned, pending = _wire(monkeypatch, FakeResp({"predictions": []}))
    out = _run([{"x1": 1.0}], pending)
    assert "prediction_ids" not in out and logged == [] and captured == [] and spawned == []


# --- what gets logged ------------------------------------------------------------------------------

def test_logs_the_numeric_score_not_the_thresholded_class(monkeypatch):
    logged, _, _, pending = _wire(monkeypatch, FakeResp(CHILD_OK))
    _run([{"x1": 1.0}, {"x1": 2.0}], pending)
    assert [r["prediction"] for r in logged] == [0.87, 0.12]   # probabilities — AUC ranks these
    assert all(r["prediction"] not in (0, 1) for r in logged)  # never the 0/1 class


def test_rows_are_logged_version_scoped_so_the_quality_window_can_join(monkeypatch):
    logged, _, _, pending = _wire(monkeypatch, FakeResp(CHILD_OK), serving=("tab-model", "7"))
    _run([{"x1": 1.0}, {"x1": 2.0}], pending)
    assert all(r["name"] == "tab-model" and r["version"] == "7" for r in logged)
    assert all(r["modality"] == "tabular" for r in logged)     # the MODALITY_TASK key


def test_feature_rows_are_captured_for_shadow_replay(monkeypatch):
    _, captured, _, pending = _wire(monkeypatch, FakeResp(CHILD_OK))
    _run([{"x1": 1.0}, {"x1": 2.0}], pending)
    assert [c[2] for c in captured] == [{"x1": 1.0}, {"x1": 2.0}]
    assert all(c[1] == "tabular" for c in captured)


def test_unresolved_version_still_returns_ids_and_does_not_raise(monkeypatch):
    """Attribution is best-effort: an unreachable/unseeded registry must not break serving."""
    logged, _, _, pending = _wire(monkeypatch, FakeResp(CHILD_OK), serving=None)
    out = _run([{"x1": 1.0}, {"x1": 2.0}], pending)
    assert len(out["prediction_ids"]) == 2
    assert all(r["name"] is None and r["version"] is None for r in logged)


# --- the score extractor ---------------------------------------------------------------------------

def test_row_scores_prefers_score_falls_back_to_prediction_and_guards_shape():
    assert tab._row_scores({"predictions": [{"prediction": 1, "score": 0.9}]}) == [0.9]
    assert tab._row_scores({"predictions": [{"prediction": 1}]}) == [1]      # older child, no score
    assert tab._row_scores({"predictions": [0.4, 0.6]}) == [0.4, 0.6]       # bare scalars
    assert tab._row_scores("not-a-dict") == [] and tab._row_scores({}) == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
