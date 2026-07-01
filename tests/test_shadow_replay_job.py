"""016 US2 — trainer-side challenger scoring (T314, FR-148).

Offline + GPU-free: the row-shaping (`replay_rows`) is pure, and `score_challenger` runs an injected
`predict_fn` (015's seam) over the window — the real artifact load under the lease is the on-hardware
seam `build_challenger_predict_fn` (SC-095/096), which raises clearly if called without the live stack.
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    path = os.path.join(REPO, "training", "flows", "shadow_replay.py")
    spec = importlib.util.spec_from_file_location("shadow_replay_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


def _pairs(inputs):
    return [{"prediction_id": f"p{i}", "input": x, "label": "l", "champion_prediction": "c", "ts": i}
            for i, x in enumerate(inputs)]


def test_replay_rows_shape_per_modality():
    assert m.replay_rows(_pairs(["hi"]), "text-generation") == [{"prompt": "hi"}]
    assert m.replay_rows(_pairs(["IMG"]), "vision") == [{"image_b64": "IMG"}]      # alias normalized
    assert m.replay_rows(_pairs(["AUD"]), "asr") == [{"audio_b64": "AUD"}]


def test_replay_rows_rejects_unsupported_modality():
    try:
        m.replay_rows(_pairs(["x"]), "embedding")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an out-of-scope modality")


def test_score_challenger_runs_injected_predict_fn():
    pairs = _pairs(["a", "b", "c"])
    seen = {}

    def predict_fn(rows, modality, version):
        seen["rows"], seen["modality"], seen["version"] = rows, modality, version
        return [r["prompt"].upper() for r in rows]

    preds = m.score_challenger(pairs, "llm", "7", predict_fn=predict_fn)
    assert preds == ["A", "B", "C"]
    assert seen["modality"] == "text-generation" and seen["version"] == "7"  # alias normalized


def test_score_challenger_length_mismatch_raises():
    try:
        m.score_challenger(_pairs(["a", "b"]), "llm", "1", predict_fn=lambda r, mod, v: ["only-one"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on a prediction/row length mismatch")


def test_build_challenger_predict_fn_rejects_unsupported_modality_before_any_fetch():
    # An out-of-scope modality is refused BEFORE the registry/MinIO fetch (fail-fast, no network): if the
    # ordering regressed, this would hang trying to reach MLflow instead of raising.
    m._version_source_and_tags = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("registry must not be consulted for an unsupported modality"))
    try:
        m.build_challenger_predict_fn("m", "1", "embedding")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an out-of-scope modality (labeled-prediction only)")


def test_build_challenger_predict_fn_dispatches_per_modality():
    # The on-hardware artifact load is stubbed; this verifies the loader resolves the version once and
    # routes each modality to its 015 scorer builder (vision→in-memory, LLM/ASR→transient server) with
    # the fetched source + provenance tags — the GPU/IO itself is exercised by SC-095/096.
    calls = {}
    m._version_source_and_tags = lambda name, version: (f"s3://models/{name}/{version}/art", {"arch": "x"})
    m._vision_predict_fn = lambda source, tags: (calls.__setitem__("vision", (source, tags)), "V")[1]
    m._llm_predict_fn = lambda source, tags: (calls.__setitem__("llm", (source, tags)), "L")[1]
    m._asr_predict_fn = lambda source: (calls.__setitem__("asr", source), "A")[1]

    assert m.build_challenger_predict_fn("m", "3", "vision") == "V"
    assert m.build_challenger_predict_fn("m", "4", "llm") == "L"
    assert m.build_challenger_predict_fn("m", "5", "asr") == "A"
    assert calls["vision"] == ("s3://models/m/3/art", {"arch": "x"})
    assert calls["llm"] == ("s3://models/m/4/art", {"arch": "x"})
    assert calls["asr"] == "s3://models/m/5/art"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
