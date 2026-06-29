# Held-out evaluation benchmarks (011)

Small, **held-out** fixtures the evaluation harness (`gateway/app/evaluation.py`) scores a model
version against to compute its modality's **primary metric** (US1, FR-100/FR-101). They are
deliberately tiny — a *comparable, reproducible* signal sized for the hardware profile (Principle
III), not a leaderboard. Each file's bytes are SHA-256 hashed by the harness so every logged
`EvalResult` records its benchmark's name + a content digest (provenance — a score is meaningless
without the set it was measured on).

| Modality | File | Rows | Primary metric (direction) | Status |
|---|---|---|---|---|
| LLM (`text-generation`) | `llm/qa_smoke.jsonl` | `{prompt, answer}` | task-accuracy (higher-better), perplexity fallback (lower) | committed |
| Vision (`image-classification`) | `vision/shapes_smoke.jsonl` | `{image_b64, label}` | top-1 accuracy (higher-better) | committed |
| ASR (`asr`) | — | `{audio_b64, text}` | WER (lower-better) | guidance stub until 009 ASR serving matures |
| Embeddings (`embedding`) | — | — | recall@k (higher-better) | guidance stub |
| Tabular (`tabular`) | — | — | AUC (higher-better) | guidance stub |

The two committed modalities (LLM, vision) are the ones the platform serves today. The stubs carry a
default metric + direction in the harness's `METRICS` registry but ship no fixture yet — add one under
the matching folder when its serving path is wired.

**Configurable.** A different fixture is passed per call (`--benchmark <path>` /
`{"benchmark": "<path>"}`); the default per modality lives in `evaluation.DEFAULT_BENCHMARKS`. Paths
resolve under this directory (override the root with `BENCHMARKS_DIR`).

**Format.** One JSON object per line (JSONL). The harness reads the reference from `answer`
(LLM), `label` (vision), or `text` (ASR).
