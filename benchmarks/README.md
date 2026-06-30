# Held-out evaluation benchmarks (011, extended 015)

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
| ASR (`asr`) | `asr/wer_smoke.jsonl` | `{audio_b64, text}` | WER (lower-better) | committed (015 — scored at registration) |
| Embeddings (`embedding`) | `embedding/recall_smoke.jsonl` | `{query, positive}` | recall@k (higher-better) | committed (015 — scored at registration) |
| Tabular (`tabular`) | — | — | AUC (higher-better) | guidance stub (no fine-tune flow) |

All four trainable modalities (LLM, vision, ASR, embeddings) score at registration as of **015** — every
fine-tune logs its modality's primary metric on the new version (see `training/scoring/`). Tabular has no
fine-tune flow, so it carries a default metric + direction in the harness's `METRICS` registry but ships
no fixture yet — add one under a `tabular/` folder if a tabular training path is ever wired.

**Configurable.** A different fixture is passed per call (`--benchmark <path>` /
`{"benchmark": "<path>"}`); the default per modality lives in `evaluation.DEFAULT_BENCHMARKS`. Paths
resolve under this directory (override the root with `BENCHMARKS_DIR`).

**Format.** One JSON object per line (JSONL). The harness reads the reference from `answer` (LLM),
`label` (vision), or `text` (ASR); the embeddings recall@k scorer uses each row's own `positive` as the
relevant document within the corpus of all positives.

**Embeddings recall@k** (`embedding/recall_smoke.jsonl`): each row is a `{query, positive}` pair. The
scorer encodes every query and the corpus of all `positive` passages, ranks the corpus per query by
cosine similarity, and counts a hit when a query's own positive lands in the top-k (k=5 default) — a
self-contained recall@k over a tiny held-out set.

**ASR WER** (`asr/wer_smoke.jsonl`): each row is a `{audio_b64, text}` pair (16 kHz mono 16-bit PCM WAV,
base64). The scorer transcribes the served ggml via a transient whisper.cpp and computes WER against
`text`. The clips are short synthetic tones — enough to exercise the served-artifact path; the score is a
smoke signal, not a leaderboard.
