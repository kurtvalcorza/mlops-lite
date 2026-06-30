"""Shadow-replay challenger scoring (016 US2) — the trainer-side GPU job.

The gateway resolves the replay window (captured ∩ labeled champion traffic) and dispatches this job. It
loads the **challenger's served artifact** under the single GPU lease (sequential — one model in VRAM,
Principle II / FR-148), runs it over the captured inputs via **015's in-process per-modality scorer**, and
returns the challenger's prediction per pair. The champion is **not** re-run (its predictions are already
logged — FR-149); the gateway pairs these challenger predictions with the logged champion predictions +
labels to build the advisory verdict.

The artifact loading (fetch the challenger version's GGUF/ggml/model.pt from the registry and build the
015 scorer) is the on-hardware seam `build_challenger_predict_fn`; offline tests inject a `predict_fn`, so
the row-shaping + orchestration here run with no GPU. Reuses 015's `training/scoring/` — no new dependency.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))           # training/flows
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # training/ (for scoring)


def replay_rows(pairs: list, modality: str) -> list:
    """Shape the replay window's recoverable inputs into the row dicts 015's per-modality scorer expects
    — **pure**. LLM → `{prompt}`, vision → `{image_b64}`, ASR → `{audio_b64}`. The input payload is the
    recoverable served input captured by 016 (016 capture-extension)."""
    field = {
        "text-generation": "prompt",
        "image-classification": "image_b64",
        "asr": "audio_b64",
    }.get(_normalize(modality))
    if not field:
        raise ValueError(f"shadow-replay does not support modality {modality!r}")
    return [{field: p["input"]} for p in pairs]


def _normalize(modality: str) -> str:
    return {"llm": "text-generation", "vision": "image-classification"}.get(modality, modality)


def score_challenger(pairs: list, modality: str, version: str, *, predict_fn) -> list:
    """Run the challenger over the replay window → one prediction per pair (same order as `pairs`).

    `predict_fn(rows, modality, version) -> [pred]` is 015's seam — built on-hardware by
    `build_challenger_predict_fn` (loads the challenger artifact under the lease) or injected in tests.
    Returns the challenger predictions to hand back to the gateway's verdict builder.
    """
    rows = replay_rows(pairs, modality)
    preds = predict_fn(rows, _normalize(modality), str(version))
    if len(preds) != len(pairs):
        raise ValueError(f"challenger produced {len(preds)} predictions for {len(pairs)} replay rows")
    return list(preds)


def _load_gateway_shadow():
    """Import the gateway's shadow orchestration (window resolve + verdict + persist) from the native
    trainer — path-injected, the same cross-tree pattern hpo.py uses for 011's evaluation harness."""
    gw = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      "gateway")
    if gw not in sys.path:
        sys.path.insert(0, gw)
    from app import shadow
    return shadow


def replay_job(name, champion_version, challenger_version, modality, *, shadow_id, window_n=None):
    """Trainer-side entry for a dispatched shadow-replay: build the challenger's 015 predict_fn (loads the
    served artifact under the GPU lease — one model in VRAM, FR-148), then run the gateway's orchestration
    (resolve the captured∩labeled window → score the challenger → build the advisory verdict → persist).
    The caller (trainer daemon) holds the lease and releases it after."""
    shadow = _load_gateway_shadow()
    predict_fn = build_challenger_predict_fn(name, challenger_version, modality)

    def scorer(pairs, mod, version):
        return score_challenger(pairs, mod, version, predict_fn=predict_fn)

    return shadow.run_replay(name, champion_version, challenger_version, modality,
                             shadow_id=shadow_id, scorer=scorer, window_n=window_n)


def build_challenger_predict_fn(name: str, version: str, modality: str):
    """On-hardware seam: fetch the challenger version's **served artifact** from the registry and build
    015's per-modality `predict_fn` for it (loaded sequentially under the lease — one model in VRAM).

    This is the GPU/IO path exercised by the on-hardware SCs (SC-095/096): vision/embeddings score the
    in-memory artifact; LLM/ASR load the served GGUF/ggml via a transient llama.cpp/whisper.cpp (015's
    `training.scoring`). It is intentionally not invoked offline — tests inject a `predict_fn` into
    `score_challenger`. Raises a clear error if called without the live stack."""
    raise NotImplementedError(
        "build_challenger_predict_fn loads a registered artifact under the GPU lease — on-hardware only "
        "(SC-095/096). Offline, inject a predict_fn into score_challenger(). Wiring the per-modality "
        "artifact fetch (GGUF/ggml/model.pt by version) + 015 scorer is the reference-hardware step.")
