"""025 US1 (T596) — batch correctness, the offline-verifiable legs.

Two shipped fixes are covered here without a GPU or a live stack:
  1. the tabular batch payload — the flow must POST the child's `{"rows": [...]}` body, not the
     per-row `{"features": row}` that 422s against `serving/children/tabular_service.py` (FR-349);
  2. GPU-alias protection — an alias-named GPU batch (`text-generation`/`image-classification`) must
     trip `_gpu_batch_active` serving-holder protection, exactly like its canonical modality, or a
     promote reload / preempting request can change the engine mid-batch (FR-350).

The load-under-lease + batch-wide-exclusion + load-failure-restore + concurrent-inference legs
(FR-348/FR-350) drive a real GPU serving engine and the agent's request handling; they are the [HW]
task T599, validated on the box, not here.
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import jobs  # noqa: E402


def _load_batch_infer():
    # Load the native flow as a standalone module (Prefect absent → its decorator degrades to a no-op).
    path = os.path.join(REPO, "training", "flows", "batch_infer.py")
    spec = importlib.util.spec_from_file_location("batch_infer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- GPU-alias protection (hostagent/jobs.py) ------------------------------------------------------

def test_gpu_batch_alias_normalizes_to_protection():
    # canonical GPU modalities protect (unchanged)
    assert jobs._is_gpu_batch("llm") and jobs._is_gpu_batch("vision") and jobs._is_gpu_batch("asr")
    # the two admitted ALIASES must also protect — the gap this fixes
    assert jobs._is_gpu_batch("text-generation")       # -> llm
    assert jobs._is_gpu_batch("image-classification")  # -> vision
    # tabular is CPU/off-lease — never a GPU serving holder
    assert not jobs._is_gpu_batch("tabular")


# --- tabular batch payload (training/flows/batch_infer.py) -----------------------------------------

def test_tabular_predict_posts_rows_body_and_returns_prediction(monkeypatch):
    import httpx
    bi = _load_batch_infer()
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "t", "device": "cpu",
                    "predictions": [{"prediction": 1, "score": 0.87}]}

    def fake_post(url, json=None, timeout=None):
        captured["url"], captured["json"] = url, json
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    predict = bi._predict_fn("tabular")
    out = predict({"f1": 1.0, "f2": 2.0})

    # posts the child's batch body, NOT the {"features": row} that 422s
    assert captured["json"] == {"rows": [{"f1": 1.0, "f2": 2.0}]}
    assert captured["url"].endswith("/predict")
    # returns the single row's prediction dict (thresholded class + numeric score)
    assert out == {"prediction": 1, "score": 0.87}


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
