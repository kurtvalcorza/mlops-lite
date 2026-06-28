# Implementation Plan: Multimodal Fine-Tuning (Vision · Embeddings · ASR)

**Branch**: `010-multimodal-finetune` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/010-multimodal-finetune/spec.md` (extend the LLM-only trainer to
fine-tune vision, embeddings, and ASR, each registering a servable MLflow version with lineage)

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

**Grilled decisions (2026-06-28)**:
- **(a) Per-modality hyperparams = sensible defaults, configurable, HPO-tunable.** Conservative VRAM-fitting
  defaults per modality — vision = freeze-backbone / train-head (small LR, few epochs); embeddings =
  contrastive **MultipleNegativesRankingLoss** (in-batch negatives, few epochs); ASR = **Whisper-small + LoRA
  (PEFT)** (low LR + warmup + grad-accum to fit `VRAM_GB`) — all **exposed on the Runs form** (as the LLM
  trainer is today) and left for **012 HPO** to sweep; no exact LR/epochs/batch pinned.
- **(b) Drift → retrain per-modality = deferred (out of scope).** 010 ships the fine-tune **capability** (run
  via the Runs flow, register a servable version); auto-retrain fan-out on per-modality drift folds into **013
  (quality monitoring) / a follow-on**. In Non-Goals.
- **(c) Whisper HF → ggml = `convert-h5-to-ggml` (HF route), q8_0.** The fine-tune output is HF-transformers,
  so convert with whisper.cpp's `convert-h5-to-ggml` (HF route), q8_0 (mirrors the LLM LoRA → GGUF pattern);
  **not** the OpenAI `.pt` `convert-pt-to-ggml` route. If ASR uses LoRA, **merge the adapter into the base HF
  model before** converting.

## Summary

Extend the native-WSL trainer daemon (today LLM PEFT/LoRA → GGUF only) with three new per-modality fine-tune
flows plus lineage/adapter-chaining, each running as the **heaviest single GPU lease tenant** and registering a
servable MLflow version with serving tags: (US1) **vision** torchvision transfer-learning (freeze backbone /
swap head) → `model.pt`; (US2) **embeddings** sentence-transformers contrastive/triplet → ST model dir; (US3)
**ASR** HF Whisper-small + LoRA fine-tune → **new HF → ggml converter** (`convert-h5-to-ggml`, q8_0) →
`ggml-*.bin`; (US4) **lineage** tags +
resume-from-prior-version chaining across all flows. Deps are mostly already in the `~/mlops-train` venv → this
is **new trainer code paths**, with the Whisper ggml converter the one new tool. The **frozen GPU/FT stack is
untouched**, promotion stays manual (gated promotion is 011), and each modality re-validates the lease mutex +
the 009 serving load contract. Phase-gated like 002/004/005/006/007, never regressing, never moving the frozen
stack.

## Technical Context

**Language/Version**: Python (native WSL `~/mlops-train` venv), Prefect ephemeral flows — **unchanged runtime**.
No new language, no new service, no new always-on process.

**Primary Dependencies (mostly already present in `~/mlops-train`)**: `torchvision` (vision head-swap +
`model.pt`), `sentence-transformers` (embeddings contrastive/triplet `fit`/loss), `transformers` (Whisper
seq2seq fine-tune) + `peft`/`accelerate`/`datasets` (already used by the LLM flow), `mlflow-skinny` (run +
registry), `boto3` (MinIO). **New tooling**: the **Whisper HF → ggml converter** for whisper.cpp (a
script/tool mirroring `convert_to_gguf` — whisper.cpp's **`convert-h5-to-ggml`** HF route, quantized **q8_0**;
if ASR uses LoRA, merge the adapter into the base HF model before converting). **Frozen (NOT upgraded)**: torch `…+cu128`, torchvision
`…+cu128`, transformers, peft, accelerate, datasets — 010 adds code on top of the validated sm_120 stack.

**Lease/mutex (FR-097)**: reuse the existing `trainer.py` machinery verbatim — one-run-at-a-time `_lock`, the
`_serving_resident()` supervisor check that refuses to start while a model holds VRAM, and the failure path
(`torch.cuda.empty_cache()` + no partial version). A fine-tune is the **heaviest** tenant (weights + grads +
optimizer); the lease semantics are identical, the footprint is larger — every modality's defaults must fit
`VRAM_GB`.

**Serving load contracts (depends on 009)**: vision = `model.pt` (`{state_dict, categories}`) loaded by the
BentoML service; embeddings = a sentence-transformers model **directory**; ASR = `ggml-*.bin` loaded by the
whisper.cpp server. 010 fine-tunes *into* these exact shapes so a promoted version is servable with no server
change.

**Storage**: artifacts content-addressed on the MinIO `models` bucket (`<name>/<run_id|version>/…`), exactly
like the LoRA adapter + the vision seed; lineage + serving tags live on the MLflow model version.

**Target Platform**: Win11 + WSL2 + Rancher Desktop; the trainer + all fine-tunes run **native in WSL** on the
single GPU (hybrid-GPU, constitution v1.2.0). MLflow/MinIO run in Docker.

**Project Type**: trainer-capability increment over 009 — touches `training/trainer.py` (modality selector +
dispatch), new `training/flows/{vision_finetune,embeddings_finetune,asr_finetune}.py`, a shared lineage helper,
the new ASR ggml converter, `training/requirements.txt` (add `sentence-transformers` if not already pinned;
**no GPU-stack bump**), and the tests. No new service, no UI surface, no API contract change beyond the
trainer's `/train` modality field.

**Performance Goals**: none targeted beyond "fits `VRAM_GB` and frees the GPU promptly"; the fine-tunes must
not change serving latency or the GPU-lease hold semantics.

**Constraints**: one GPU lease tenant at a time (Principle II / v1.4.0); frozen GPU/FT stack; manual promotion
(gated promotion deferred to 011); every fine-tune tracked + reproducible (Principle VI); no new
service/runtime.

## Constitution Check

*GATE: Must pass before design. Re-check after. (Checked against the post-008 lease model, constitution
v1.4.0.)*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | All fine-tuning runs native in WSL on the one host; artifacts to local MinIO | ✅ |
| II. Single-GPU / one tenant (NON-NEGOTIABLE) | Each fine-tune is the **heaviest** lease tenant — reuses the trainer mutex; refuses while serving resident, frees on failure | ✅ holds per fine-tune |
| III. Lightweight Footprint | No new service/runtime; ephemeral Prefect, lazy heavy imports; small base sizes within `VRAM_GB` | ✅ |
| IV. Full Lifecycle Coverage | **Strengthens** training — fine-tuning now spans all served modalities, closing train→register→serve for each | ✅ strengthened |
| V. OSS & Swappable | torchvision / sentence-transformers / transformers / whisper.cpp — all OSS, behind the registry tag interface | ✅ |
| VI. Reproducibility & Observability | Every fine-tune logs params/metrics to MLflow + records lineage/serving tags; chaining makes genealogy explicit | ✅ strengthened |
| VII. Phase-Gated Delivery | Four independently-runnable stories (US1 vision → US2 embeddings → US3 ASR → US4 lineage), each re-validated on hardware | ✅ |
| Lease model (v1.4.0): "training is a lease tenant" | A fine-tune is exactly that — the heaviest tenant; no new lease, no new lock | ✅ already covered |
| Workflow: "no new runtime without amendment" | None introduced (existing native Python venv; deps mostly present) | ✅ no amendment |

**No amendment required.** 010 adds trainer code paths within the existing lease model (v1.4.0 already makes
training a GPU-lease tenant) and the existing native-WSL Python runtime; it advances Principles IV and VI and
leaves Principle II's one-tenant rule intact (a fine-tune simply holds the whole GPU). Clean gate-check,
mirroring 005/006/007.

## Project Structure

### Source Code (delta over 009)

```text
mlops-lite/
├── training/
│   ├── trainer.py                       # MODIFIED: /train gains a `modality` selector; dispatch to the right flow
│   ├── flows/
│   │   ├── finetune.py                  # MODIFIED: adopt the shared lineage/chaining helper (LLM flow unchanged otherwise)
│   │   ├── vision_finetune.py           # NEW (US1): torchvision freeze-backbone + swap-head → model.pt → register (bentoml tag)
│   │   ├── embeddings_finetune.py       # NEW (US2): sentence-transformers contrastive/triplet → ST dir → register
│   │   ├── asr_finetune.py              # NEW (US3): HF Whisper-small+LoRA fine-tune → merge → HF→ggml convert → register (whisper.cpp tag)
│   │   └── lineage.py                   # NEW (US4): shared lineage tags + resume-from-prior-version (chaining) helper
│   ├── tools/
│   │   └── convert_whisper_to_ggml.py   # NEW (US3): the new HF→ggml converter (convert-h5-to-ggml, q8_0; mirrors convert_to_gguf)
│   └── requirements.txt                 # MODIFIED: ensure sentence-transformers pinned; GPU/FT stack LEFT FROZEN (cu128 index)
├── scripts/
│   └── native_env.lock                  # MODIFIED: record any newly-pinned native dep (e.g. sentence-transformers); torch family unchanged
└── tests/                               # NEW per-modality tests: test_vision_finetune / test_embeddings_finetune /
                                         #   test_asr_finetune / test_lineage_chaining (+ lease-mutex assertions)
```

**Structure Decision**: one flow file per modality (so a regression bisects to a modality) behind a single
trainer dispatch; a shared `lineage.py` so tagging/chaining is identical across all four flows (incl. the
existing LLM one). The only **new tool** is the Whisper ggml converter. No server code changes (009 owns the
servers); 010 only produces the artifacts they load.

## Phasing (maps to constitution VII)

- **Phase 0 — Pre-flight**: confirm `sentence-transformers` (and the Whisper deps) resolve in `~/mlops-train`
  **without** moving the frozen torch/cu128 family; smoke whisper.cpp's **`convert-h5-to-ggml`** (HF route,
  q8_0) on a stock Whisper checkpoint; confirm the 009 serving load contracts (vision `model.pt`+categories,
  ST dir, `ggml-*.bin`).
- **Phase 1 — Vision (US1, P1)**: `vision_finetune.py` (freeze backbone, swap head, train, write `model.pt` +
  categories, upload, register with `task=image-classification`/`serving_engine=bentoml`/lineage); add the
  `modality` selector + dispatch to `trainer.py`; re-validate lease 409 + 009 vision serves new labels. Exit:
  SC-057 + the lease half of SC-061.
- **Phase 2 — Embeddings (US2, P2)**: `embeddings_finetune.py` (sentence-transformers contrastive/triplet, ST
  dir, register `task=embeddings`); 009 embeddings serves the fine-tuned vectors. Exit: SC-058.
- **Phase 3 — ASR (US3, P3)**: `asr_finetune.py` (HF Whisper-small + LoRA seq2seq, configurable defaults) +
  `tools/convert_whisper_to_ggml.py` (merge LoRA → HF→ggml via `convert-h5-to-ggml` q8_0, fail-on-nonzero,
  register `task=asr`/`serving_engine=whisper.cpp`/`format=ggml`); 009 whisper.cpp transcribes with it. Exit:
  SC-059.
- **Phase 4 — Lineage & chaining (US4, P2)**: `lineage.py` adopted by all four flows (`base` vs `chained`,
  `parent_version`, parent-run link); resume-from-prior-version (`is_trainable` / prior-checkpoint load), reject
  cross-modality parents. Exit: SC-060.
- **Cross-cutting**: every modality's default config fits `VRAM_GB` with hyperparams as MLflow params (SC-062);
  a full no-regression sweep + frozen-stack check (no torch/transformers movement) closes the increment
  (SC-063). Promotion stays manual; gated promotion is 011.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| One flow file per modality behind a `modality` dispatch | A regression bisects to a single modality; the daemon stays one single-tenant lock | A single mega-flow with branches couples three unrelated training stacks and muddies the bisect |
| Reuse the existing trainer mutex (not a new lease) | v1.4.0 already makes training a GPU-lease tenant; the LLM flow's `_serving_resident()` + `_lock` already enforce Principle II | A per-modality lease/lock would duplicate the mutex and risk two tenants — the exact thing Principle II forbids |
| New Whisper HF→ggml converter (`convert-h5-to-ggml`, q8_0; the one new tool) | whisper.cpp loads `ggml-*.bin`; there is no existing ggml path (the LoRA→GGUF tool is LLM-only); the fine-tune output is HF-transformers, so the HF route avoids an extra HF→OpenAI `.pt` step | Serving Whisper from raw PyTorch would add a heavyweight always-on PyTorch ASR server (footprint, Principle III); the `.pt` `convert-pt-to-ggml` route would need an extra HF→OpenAI conversion |
| Vision = transfer learning (freeze + swap head) | Lowest-risk new modality; tiny trainable surface fits `VRAM_GB` and trains fast; mirrors the seed model's load shape | Full-network vision training is heavier and needless for the head-swap classifier 009 serves |
| Shared `lineage.py` across all four flows | Identical tagging/chaining everywhere is the precondition for gated promotion (011) reasoning about "newer than serving" | Per-flow ad-hoc tags drift and make genealogy unreliable |
| Freeze the GPU/FT stack (add code, not versions) | torch/transformers were hard-won on sm_120+cu128; 010's value is new code paths, not a stack move | A "bump while we're here" churns the most fragile part of the stack for no 010 benefit |
| Manual promotion (defer gated promotion to 011) | 010 is large enough (three modalities + a new converter); gating is a separable concern | Bundling gated promotion now widens scope and blocks the modality work behind a policy design |
