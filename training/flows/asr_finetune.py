"""ASR (Whisper) fine-tune flow (010 US3, T191/T193 — FR-092/FR-094; the hardest modality).

Fine-tunes a HF `transformers` **Whisper-small + LoRA (PEFT)** seq2seq model on a pinned
audio+transcript dataset, then **merges the LoRA adapter into the base** and converts the merged HF
model to a quantized **q8_0 `ggml-*.bin`** (via `tools/convert_whisper_to_ggml.py`, the new HF→ggml
tool) so 009's whisper.cpp server loads it. The flow logs train loss + a **WER-style** eval metric, and
registers a version tagged `task=asr` / `serving_engine=whisper.cpp` / `format=ggml` / lineage.

Defaults are conservative VRAM-fitting (Whisper-small, LoRA on the attention projections, low LR +
warmup + grad-accum) — **exposed on the Runs form** (configurable, HPO-tunable by 012), not hard-pinned
(FR-098). Like every fine-tune it is the heaviest single GPU lease tenant; the trainer daemon holds the
lease, so the flow just trains, converts, and frees the GPU. A converter failure registers **no**
version (FR-093). Dataset shape: JSONL rows `{"audio_b64": <16 kHz mono WAV bytes b64>, "text": <str>}`.
"""
import base64
import io
import os
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _common/lineage import as module or script
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from _common import MLFLOW_URI, MODELS_BUCKET, _log, fetch_jsonl, flow, free_cuda, s3_client  # noqa: E402
from lineage import lineage_tags  # noqa: E402  (ASR records lineage; it can't chain — see the flow)
from convert_whisper_to_ggml import convert_whisper_to_ggml  # noqa: E402  (the new HF→ggml tool)

TASK = "asr"
DEFAULT_BASE = os.getenv("ASR_BASE_MODEL", "openai/whisper-small")
TARGET_SR = 16000  # Whisper's required sample rate


def _decode_wav(b64: str):
    """Decode a base64 16 kHz mono PCM WAV → float32 samples in [-1, 1].

    Uses stdlib `wave` (no librosa/soundfile dep). Whisper requires 16 kHz mono; a mismatch errors
    clearly rather than silently mis-transcribing (Edge: dataset shape mismatch)."""
    import numpy as np

    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        sr, ch, sampwidth, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sampwidth != 2:
        raise ValueError(f"WAV must be 16-bit PCM (got sampwidth={sampwidth}); re-encode the dataset")
    samples = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)  # downmix to mono
    if sr != TARGET_SR:
        raise ValueError(f"WAV must be {TARGET_SR} Hz mono (got {sr} Hz); re-encode the dataset")
    return samples


def _wer(ref: str, hyp: str) -> float:
    """Word error rate via word-level Levenshtein (no jiwer dep). 0.0 = exact; ~1.0 = all wrong."""
    r, h = ref.lower().split(), hyp.lower().split()
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / len(r)


def _parse_rows(rows: list):
    """Validate the ASR dataset shape → (audios[np.float32], texts[str])."""
    audios, texts = [], []
    for i, r in enumerate(rows):
        b64, text = r.get("audio_b64"), r.get("text")
        if not b64 or text is None:
            raise ValueError(f"ASR row {i} missing audio_b64/text — expected audio+transcript")
        audios.append(_decode_wav(b64))
        texts.append(str(text))
    if not audios:
        raise ValueError("ASR dataset has no usable {audio_b64,text} rows")
    return audios, texts


def _train_and_merge(audios, texts, out_dir, *, base_model, lr, epochs,
                     grad_accum, warmup_ratio, lora_r, seed):
    """Whisper-small + LoRA fine-tune; returns (merged_hf_dir, metrics). Merges the adapter into the
    base before returning so the converter sees a plain HF model (FR-093)."""
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    torch.manual_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float32  # keep fp32 on the tiny demo; bf16 is an HPO knob (012)
    _log(f"ASR fine-tune on {device}: {len(audios)} clips, base={base_model}, lora_r={lora_r}")

    processor = WhisperProcessor.from_pretrained(base_model)
    model = WhisperForConditionalGeneration.from_pretrained(base_model, torch_dtype=dtype)
    model.config.forced_decoder_ids = None
    model.config.suppress_tokens = []

    model = get_peft_model(model, LoraConfig(
        r=lora_r, lora_alpha=lora_r * 2, lora_dropout=0.05, bias="none",
        target_modules=["q_proj", "v_proj"]))  # attention projections (HF Whisper-PEFT recipe)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    model = model.to(device)
    _log(f"LoRA trainable params={trainable:,}")

    feats = [processor.feature_extractor(a, sampling_rate=TARGET_SR, return_tensors="pt").input_features[0]
             for a in audios]
    label_ids = [processor.tokenizer(t, return_tensors="pt").input_ids[0] for t in texts]

    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr)
    total_steps = max(epochs * len(feats), 1)
    warmup = max(int(total_steps * warmup_ratio), 1)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warmup))  # linear warmup, then flat

    model.train()
    last_loss, step = 0.0, 0
    for ep in range(epochs):
        opt.zero_grad()
        for i in range(len(feats)):
            inp = feats[i].unsqueeze(0).to(device, dtype=dtype)
            lab = label_ids[i].unsqueeze(0).to(device)
            loss = model(input_features=inp, labels=lab).loss / grad_accum
            loss.backward()
            last_loss = float(loss) * grad_accum
            if (i + 1) % grad_accum == 0 or i == len(feats) - 1:
                opt.step()
                sched.step()
                opt.zero_grad()
                step += 1
        _log(f"  epoch {ep + 1}/{epochs}: loss={last_loss:.4f}")

    # WER-style eval on the training clips (tiny demo; 013 owns a held-out eval set).
    model.eval()
    wers = []
    with torch.no_grad():
        for i in range(len(feats)):
            inp = feats[i].unsqueeze(0).to(device, dtype=dtype)
            ids = model.generate(input_features=inp, max_new_tokens=128)
            hyp = processor.batch_decode(ids, skip_special_tokens=True)[0]
            wers.append(_wer(texts[i], hyp))
    mean_wer = sum(wers) / max(len(wers), 1)

    # Merge the LoRA adapter into the base so the converter sees a plain HF Whisper model (grilled c).
    merged = model.merge_and_unload()
    merged.save_pretrained(out_dir)
    processor.save_pretrained(out_dir)

    metrics = {"train_loss": last_loss, "wer": mean_wer, "epochs": epochs,
               "trainable_params": int(trainable), "device": device}
    del model, merged
    free_cuda()
    _log(f"ASR training done: loss={last_loss:.4f} wer={mean_wer:.4f}; merged HF model -> {out_dir}")
    return out_dir, metrics, device


def _register(output_name, ggml_path, run_id, *, base_model, dataset_name, dataset_version,
              parent_version, parent_run_id, device):
    """Upload the q8_0 `ggml-*.bin` to MinIO and register a `task=asr` whisper.cpp version + lineage
    (FR-094). The whisper.cpp server loads the ggml from a local path; the registry entry is the
    routing pointer (mirrors scripts/seed_asr_model.py)."""
    from mlflow.exceptions import MlflowException
    from mlflow.tracking import MlflowClient

    key = f"{output_name}/{run_id}/{os.path.basename(ggml_path)}"
    with open(ggml_path, "rb") as f:
        s3_client().put_object(Bucket=MODELS_BUCKET, Key=key, Body=f.read())
    source = f"s3://{MODELS_BUCKET}/{key}"

    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.create_registered_model(output_name)
    except MlflowException:
        pass
    tags = {"kind": "asr", "format": "ggml", "task": TASK, "serving_engine": "whisper.cpp",
            "device": device,
            **lineage_tags(base_model, dataset_name, dataset_version, parent_version, parent_run_id)}
    mv = c.create_model_version(name=output_name, source=source, run_id=run_id, tags=tags)
    _log(f"registered {output_name} v{mv.version} <- run {run_id} (task={TASK}, format=ggml, "
         f"NOT auto-promoted — promotion stays manual)")
    return {"name": output_name, "version": str(mv.version), "source": source}


def _score_at_registration(mv, ggml_path):
    """015 (FR-137/139): score the just-registered ASR version on the served ggml. ASR is the LLM's twin
    — score the quantized ggml artifact via a transient whisper-cli (D6), with the HF training model
    already freed by _train_and_merge (one model in VRAM, Principle II) — and log WER on the version.
    Scoring failure warns + registers without a metric (does not fail the run)."""
    training_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if training_root not in sys.path:
        sys.path.insert(0, training_root)
    from scoring import asr, score_at_registration
    return score_at_registration(
        mv["name"], mv["version"], "asr", asr.make_predict_fn(ggml_path), log_fn=_log)


@flow(name="asr-finetune")
def asr_finetune_flow(dataset_name: str, dataset_version: str, output_name: str,
                      base_model: str | None = DEFAULT_BASE, epochs: int = 3, lr: float = 1e-4,
                      grad_accum: int = 4, warmup_ratio: float = 0.1, lora_r: int = 8, seed: int = 0,
                      quant: str = "q8_0", parent_version: str | None = None) -> dict:
    """End-to-end ASR fine-tune → merge → HF→ggml(q8_0) → registered, servable `task=asr` version.

    `parent_version` chains from a prior registered ASR adapter version of `output_name` (US4); a
    parent whose `task` isn't asr is rejected before training (`resolve_parent`).
    """
    import mlflow

    base = base_model or DEFAULT_BASE
    # ASR records lineage but **cannot chain from a registered version**: the stored artifact is the
    # quantized ggml binary, not a trainable PEFT adapter, so there is no reloadable checkpoint to
    # resume from (vision/embeddings are the resumable modalities — FR-096). Reject upfront with an
    # accurate message (Claude review F2 — the old s3:// guard always fired and implied a non-existent
    # local-path workaround).
    if parent_version:
        raise NotImplementedError(
            "ASR chaining is not supported: the registered artifact is the ggml binary, not a "
            "trainable PEFT adapter. Chaining is available for vision and embeddings.")

    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("asr-finetune")
    with mlflow.start_run() as run:
        run_id = run.info.run_id
        params = dict(modality="asr", base_model=base, dataset_name=dataset_name,
                      dataset_version=dataset_version, output_name=output_name, epochs=epochs,
                      lr=lr, grad_accum=grad_accum, warmup_ratio=warmup_ratio, lora_r=lora_r,
                      seed=seed, quant=quant, parent_version=None)
        mlflow.log_params(params)

        rows = fetch_jsonl(dataset_name, dataset_version)
        audios, texts = _parse_rows(rows)
        try:
            with tempfile.TemporaryDirectory(prefix="asr-") as tmp:
                merged_dir = os.path.join(tmp, "hf")
                merged_dir, metrics, device = _train_and_merge(
                    audios, texts, merged_dir, base_model=base, lr=lr,
                    epochs=epochs, grad_accum=grad_accum, warmup_ratio=warmup_ratio, lora_r=lora_r,
                    seed=seed)
                mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
                mlflow.set_tag("device", device)
                # Convert AFTER metrics are logged but BEFORE registering — a converter failure raises
                # and registers no version (FR-093), while the run still carries the recorded loss/WER.
                ggml = os.path.join(tmp, f"ggml-{output_name}-{quant}.bin")
                convert_whisper_to_ggml(merged_dir, ggml, quant=quant)
                mv = _register(output_name, ggml, run_id, base_model=base, dataset_name=dataset_name,
                               dataset_version=dataset_version, parent_version=None,
                               parent_run_id=None, device=device)
                eval_result = _score_at_registration(mv, ggml)
                if eval_result:
                    mlflow.set_tag("eval_metric", f"{eval_result['metric']}={eval_result['value']}")
        finally:
            free_cuda()  # release VRAM whether training/convert/register succeeded or raised (Principle II)
        mlflow.set_tag("registered_version", mv["version"])
        return {"run_id": run_id, "model": mv, "metrics": metrics, "params": params,
                "eval": eval_result}


if __name__ == "__main__":
    import json
    a = sys.argv
    print(json.dumps(asr_finetune_flow(
        dataset_name=a[1], dataset_version=a[2], output_name=a[3],
        epochs=int(a[4]) if len(a) > 4 else 3), indent=2))
