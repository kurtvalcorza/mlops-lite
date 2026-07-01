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
    The caller (trainer daemon) holds the lease and releases it after.

    The challenger artifact is built **lazily, inside `scorer`** — `run_replay` runs its own `prepare()`
    guard and only invokes `scorer` when the window is `ready`, returning early (no scoring) on
    `insufficient_data`/`no_corpus`/`inputs_not_captured`. Building the `predict_fn` here (which, for
    vision, loads the model onto the GPU, and for LLM/ASR downloads the artifact) up front would leave it
    resident/on-disk on any guard-fail path, because the closures' cleanup `finally` only runs once
    `predict_fn` is actually called. Deferring the build until `scorer` fires ties the load and its
    cleanup to the same ready-and-scoring path — one model in VRAM, nothing left behind (Principle II)."""
    shadow = _load_gateway_shadow()

    def scorer(pairs, mod, version):
        predict_fn = build_challenger_predict_fn(name, challenger_version, modality)
        return score_challenger(pairs, mod, version, predict_fn=predict_fn)

    return shadow.run_replay(name, champion_version, challenger_version, modality,
                             shadow_id=shadow_id, scorer=scorer, window_n=window_n)


def build_challenger_predict_fn(name: str, version: str, modality: str):
    """On-hardware seam: fetch the challenger version's **served artifact** from the registry and build
    015's per-modality `predict_fn` for it (loaded sequentially under the lease — one model in VRAM).

    This is the GPU/IO path exercised by the on-hardware SCs (SC-095/096): vision scores the in-memory
    artifact (the trained classifier *is* the served model); LLM/ASR load the served GGUF/ggml via a
    transient llama.cpp/whisper.cpp (015's `training.scoring`). It is intentionally not invoked offline —
    tests inject a `predict_fn` into `score_challenger`; the heavy imports here are all deferred so this
    module still imports on any box. Scope mirrors `replay_rows` (labeled-prediction modalities only).

    All three per-modality builders return a `predict_fn(rows, modality, version)` that scores the
    challenger and then releases what it loaded (LLM/ASR tear down the transient server + delete the
    fetched artifact; vision drops the model ref + frees CUDA), so nothing stays resident past the replay.
    """
    mod = _normalize(modality)
    if mod not in ("image-classification", "text-generation", "asr"):
        raise ValueError(
            f"shadow-replay does not support modality {modality!r} — labeled-prediction modalities only "
            f"(text-generation / image-classification / asr)")
    source, tags = _version_source_and_tags(name, version)  # network only after the modality is valid
    if mod == "image-classification":
        return _vision_predict_fn(source, tags)
    if mod == "text-generation":
        return _llm_predict_fn(source, tags)
    return _asr_predict_fn(source)


def _version_source_and_tags(name: str, version: str):
    """(source, tags) for a registry version — the challenger's `s3://` artifact URI + its provenance
    tags (`arch` for vision, `base_model` for the LLM). Mirrors how the 015 flows read a version."""
    from _common import MLFLOW_URI
    from mlflow.tracking import MlflowClient

    mv = MlflowClient(tracking_uri=MLFLOW_URI).get_model_version(name, str(version))
    return mv.source, dict(mv.tags or {})


def _download_source(source: str) -> bytes:
    """Fetch a registry `s3://bucket/key` artifact from MinIO → bytes (mirrors vision `_warm_start`)."""
    from _common import s3_client

    if not source or not source.startswith("s3://"):
        raise ValueError(f"unexpected registry source {source!r} — expected s3://bucket/key")
    bucket, key = source[len("s3://"):].split("/", 1)
    return s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()


def _vision_predict_fn(source: str, tags: dict):
    """Rebuild the served vision classifier in-memory from its `model.pt` {state_dict, categories} (the
    009 BentoML load shape) and score in-process — the trained model IS the served artifact, no
    quantization gap (015 D6). Drops the model + frees CUDA after the single scoring call (Principle II)."""
    import io
    import os as _os

    import torch

    from vision_finetune import _build_model

    # weights_only=False: model.pt carries the `categories` list alongside the state_dict, so the
    # torch>=2.6 weights-only default would refuse it — mirrors vision `_warm_start`.
    payload = torch.load(io.BytesIO(_download_source(source)), map_location="cpu", weights_only=False)
    state_dict, categories = payload["state_dict"], list(payload["categories"])
    backbone = tags.get("arch") or _os.getenv("VISION_BASE_ARCH", "mobilenet_v2")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = _build_model(backbone, len(categories), pretrained=False)
    model.load_state_dict(state_dict)
    model = model.to(device)
    model.eval()
    holder = [model]

    def predict_fn(rows, _modality, _version):
        from scoring import vision
        try:
            return vision.make_predict_fn(holder[0], categories, device)(rows, _modality, _version)
        finally:
            holder[0] = None  # drop the only surviving ref so GC + empty_cache can reclaim VRAM
            _free_cuda()

    return predict_fn


def _llm_predict_fn(source: str, tags: dict):
    """Download the challenger's LoRA-GGUF adapter and build 015's transient-llama.cpp scorer against the
    served base GGUF + adapter (`base_model` from lineage tags). The scorer tears the server down per call;
    we delete the fetched adapter afterwards so nothing lingers."""
    from scoring import llm

    tmp, adapter = _fetch_to_tmp(source, "shadow-llm-", "adapter.gguf")
    base_model = tags.get("base_model") or os.getenv("BASE_MODEL", llm.DEFAULT_BASE_HF)
    inner = llm.make_predict_fn(adapter, base_model)
    return _cleanup_after(inner, tmp)


def _asr_predict_fn(source: str):
    """Download the challenger's `ggml-*.bin` and build 015's transient-whisper.cpp scorer against it
    (load → transcribe → free per clip). Deletes the fetched ggml after the scoring call."""
    from scoring import asr

    tmp, ggml = _fetch_to_tmp(source, "shadow-asr-", os.path.basename(source.rstrip("/")) or "model.bin")
    inner = asr.make_predict_fn(ggml)
    return _cleanup_after(inner, tmp)


def _fetch_to_tmp(source: str, prefix: str, filename: str):
    """Download `source` into a fresh temp dir under `filename`; return (tmpdir, filepath). The file must
    outlive `build_challenger_predict_fn` (llama.cpp/whisper.cpp read it at predict time), so the dir is
    cleaned by `_cleanup_after` once the single scoring call completes."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix=prefix)
    path = os.path.join(tmp, filename)
    with open(path, "wb") as f:
        f.write(_download_source(source))
    return tmp, path


def _cleanup_after(inner, tmpdir: str):
    """Wrap a per-call `predict_fn` so its downloaded artifact dir is removed after the (single) call."""
    def predict_fn(rows, _modality, _version):
        try:
            return inner(rows, _modality, _version)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    return predict_fn


def _free_cuda():
    from _common import free_cuda
    free_cuda()
