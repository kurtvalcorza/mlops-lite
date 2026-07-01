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


def test_build_challenger_predict_fn_rejects_kind_mismatch_before_dispatch():
    # A registered version whose artifact `kind` doesn't match the requested modality (e.g. a vision
    # model.pt asked to replay as an LLM) is refused BEFORE any per-modality builder runs — no fetch, no
    # GPU load, no lease taken. Guards a registered name that carries versions of different tasks.
    m._version_source_and_tags = lambda name, version: (
        f"s3://models/{name}/{version}/model.pt", {"kind": "vision-classifier"})
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no builder may run on a kind mismatch"))
    m._vision_predict_fn = m._llm_predict_fn = boom
    m._asr_predict_fn = boom
    try:
        m.build_challenger_predict_fn("m", "1", "llm")
    except ValueError as e:
        assert "cannot shadow-replay" in str(e)
    else:
        raise AssertionError("expected ValueError on a kind/modality mismatch")


def _fake_shadow(status):
    """A stand-in for the gateway's shadow orchestration: `run_replay` mirrors the real guard — it calls
    `scorer` only when the window is `ready`, and returns early (no scoring) otherwise."""
    class _S:
        @staticmethod
        def run_replay(name, champion, challenger, modality, *, shadow_id, scorer, window_n=None):
            if status != "ready":
                return {"status": status, "shadow_id": shadow_id, "name": name}
            return {"status": "ready", "verdict": scorer([{"input": "x", "label": "l"}], modality, challenger)}
    return _S


def test_replay_job_builds_challenger_only_when_scored():
    # Guard-fail path (e.g. the window shrank below min between the gateway's check and the trainer's
    # re-check): run_replay never calls scorer, so the challenger artifact must NOT be built — otherwise
    # the eager GPU load / temp-dir download leaks (its cleanup lives in the never-called closure).
    built = []
    m._load_gateway_shadow = lambda: _fake_shadow("insufficient_data")
    m.build_challenger_predict_fn = lambda *a, **k: built.append(a) or (lambda rows, mod, v: [])
    out = m.replay_job("mdl", "1", "2", "vision", shadow_id="s1")
    assert out["status"] == "insufficient_data"
    assert built == [], "challenger artifact must not be built on a guard-fail (non-ready) path"

    # Ready path: scorer fires, so the challenger IS built (exactly once) and scored.
    m._load_gateway_shadow = lambda: _fake_shadow("ready")
    m.build_challenger_predict_fn = lambda *a, **k: built.append(a) or (lambda rows, mod, v: ["PRED"])
    out = m.replay_job("mdl", "1", "2", "vision", shadow_id="s2")
    assert out["status"] == "ready" and out["verdict"] == ["PRED"]
    assert len(built) == 1, "challenger built exactly once, on the ready-and-scoring path"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
