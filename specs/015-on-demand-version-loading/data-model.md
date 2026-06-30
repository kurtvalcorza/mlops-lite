# Phase 1 Data Model: Score-at-Registration (015)

015 adds **no new persistent store**. It reuses 011's MLflow tag/metric schema and the eval harness's
seams. Two conceptual entities + one contract.

## Entity: Registration-time EvalResult

The eval metric logged against a model version **at registration** (not a separate evaluate call). Schema
is exactly 011's `EvalResult` (reused via `evaluation._log_eval`), so the gate/compare/quality read it
unchanged.

| Field | Source | Notes |
|---|---|---|
| `metric` | per-modality primary metric | task_accuracy (LLM), top-1 accuracy (vision), recall@k (embeddings), WER (ASR) |
| `value` | the in-process score | float, rounded; logged as version tag + a run metric `eval_<metric>` |
| `direction` | metric registry | higher/lower-better — drives the gate (011) |
| `modality` | the fine-tune's modality | text-generation / image-classification / embedding / asr |
| `benchmark` | the fixture name | e.g. `llm/qa_smoke.jsonl`, `embedding/recall_smoke.jsonl`, `asr/wer_smoke.jsonl` |
| `benchmark_hash` | SHA-256 of fixture bytes | provenance (a score is meaningless without its set) |

- **Written**: by each fine-tune flow, **before lease release**, on the newly-registered version.
- **Read**: by `compare()`, the promotion gate, quality (013), and the HPO objective — all via 011's
  `read_eval`. No reload of any model.

## Entity: Per-modality in-process scorer (`predict_fn`)

A trainer-side function matching the eval harness's existing seam
`predict_fn(rows, modality, version) -> list[prediction]`, implemented per modality:

| Modality | Scorer | Served-artifact? |
|---|---|---|
| text-generation (LLM) | load base GGUF + LoRA-GGUF adapter in a **transient llama.cpp**, generate over QA prompts | **yes** (D5) |
| asr | transcribe the WER fixture via the served **ggml** in a **transient whisper.cpp** | **yes** (D6) |
| image-classification | run the in-memory torch model over the benchmark images → labels | in-memory == served |
| embedding | encode with the in-memory sentence-transformers model → vectors → recall@k | in-memory == served |

- Lives in a new `training/scoring/` module; the metric math itself is **011's pure-Python** functions
  (`task_accuracy`, `accuracy`, `recall_at_k`, `wer`) imported from `gateway/app/evaluation.py`.
- Invoked by a thin `score_and_log(version, modality, ...)` the fine-tune flow calls inside the lease hold.

## State transition (per fine-tune, within one GPU-lease hold)

```
acquire lease → train (model in VRAM)
             → free training model (+optimizer, empty_cache)        # one-model-in-VRAM invariant
             → [LLM/ASR] load served artifact (GGUF/ggml) → score → free
               [vision/embeddings] score in-memory model → free
             → log EvalResult on the new version
             → release lease
```

## Contract surface (see contracts/)

Only one external contract changes — gateway `POST /models/{name}/evaluate` gains a **guard** (FR-143).
`compare`/gate/quality contracts are unchanged (they already read logged metrics).
