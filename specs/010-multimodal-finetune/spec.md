# Feature Specification: Multimodal Fine-Tuning (Vision · Embeddings · ASR)

**Feature Branch**: `010-multimodal-finetune`

**Created**: 2026-06-28

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

**Input**: The trainer (`training/trainer.py` + `training/flows/finetune.py`) today fine-tunes **LLMs only**
(PEFT/LoRA → GGUF → register). Increment 009 added **serving** for the non-LLM modalities (vision /
embeddings / ASR); 010 closes the loop by letting the platform **fine-tune** each of those modalities and
register a servable version — so every served modality is also a *trainable* one. Each fine-tune is the
**heaviest GPU lease tenant** (full VRAM: weights + grads + optimizer state), mutually excluding ALL serving
under the single-GPU lease (Principle II / the v1.4.0 lease model). Requirement IDs continue the shared space
(FR-088+, SC-057+, tasks T180+). **No constitution amendment** — training-as-a-lease-tenant is already covered
by v1.4.0, and the work uses the existing native-WSL Python runtime + already-present deps. See plan.md →
Constitution Check.

> **Scope note**: 010 is a **trainer-capability** increment. It adds **no new service, no new runtime, and no
> new always-on process** — it extends the existing ephemeral-Prefect native trainer daemon with three new
> per-modality fine-tune flows (vision, embeddings, ASR) plus lineage/adapter-chaining, each registering an
> MLflow version with serving tags (`task`, `serving_engine`, base/lineage) so 009's servers can load it. The
> deps are **mostly already present** in the `~/mlops-train` venv (`transformers`/`peft`/`accelerate`/
> `datasets`/`torchvision`/`sentence-transformers`) → this is mostly **new trainer code paths**, not new heavy
> deps. The one genuinely new piece of tooling is the **Whisper HF → ggml converter** for whisper.cpp
> serving (whisper.cpp's `convert-h5-to-ggml`, q8_0; mirrors the existing LoRA → GGUF converter).

> **Hard boundary (NON-NEGOTIABLE)**: the frozen **Blackwell sm_120 GPU stack** (`torch==…+cu128`,
> `torchvision==…+cu128`, `transformers`/`peft`/`accelerate`/`datasets`) is **NOT upgraded** in 010 — 010 adds
> code paths on top of the validated stack, it does not churn it. **Principle II holds per fine-tune**: a
> running fine-tune holds the **whole GPU** (it is the lease tenant), so it refuses to start while a model is
> resident in serving and serving refuses to start while it runs — exactly as the LLM LoRA flow does today.

> **Gating note**: 010 lands fine-tuned versions in the registry with serving tags; **promotion to `@serving`
> remains operator-driven** (alias-based, as since 004). The **gated/automated promotion** workflow is a later
> increment (**011**) and is explicitly out of scope here.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Vision transfer-learning fine-tune (Priority: P1, easiest)

The operator fine-tunes an image classifier by **transfer learning** on a pinned dataset version: load a
torchvision backbone, **freeze the backbone and swap the classifier head** for the dataset's class count,
fine-tune the head (optionally unfreeze for a low-LR pass), write a new `model.pt` (state_dict + the new
`categories`), upload to MinIO, and register a new MLflow version tagged `task=image-classification`,
`serving_engine=bentoml`, with lineage — so 009's BentoML vision service can load it straight from the
registry. The **default** approach is freeze-backbone / train-head with a small LR and few epochs (a
conservative VRAM-fitting default); these hyperparameters are **exposed on the Runs form** (configurable, like
the LLM trainer today) and left for **012 HPO** to sweep — the spec does not pin exact LR/epochs/batch.

**Why this priority**: Vision is the **lowest-risk** new modality — torchvision is already a dep, the serving
path (009 BentoML, CPU) already loads a `model.pt` + `categories` from the `models` bucket, and the seed model
(`scripts/seed_vision_model.py`) already shows the exact registration shape to mirror. It de-risks the
mutex/lease, registry-tag, and lineage mechanics for the harder modalities, so it leads.

**Independent Test**: With the serving model idle (GPU free), POST a vision fine-tune (dataset name+version,
base arch, frozen backbone, swapped head); it runs on the GPU as a lease tenant, logs params/metrics to
MLflow, writes a `model.pt` carrying the new head + `categories`, registers a version tagged
`task=image-classification` / `serving_engine=bentoml` / lineage; promoting it to `@serving` makes 009's vision
service classify into the **new** label set. Starting it while a model is GPU-resident returns 409.

**Acceptance Scenarios**:

1. **Given** a pinned image-classification dataset and a torchvision base, **When** a vision fine-tune runs with
   the backbone frozen and the head swapped to the dataset's class count, **Then** it completes on the GPU,
   logs train/val metrics to MLflow, and registers a `model.pt`-backed version with `task=image-classification`,
   `serving_engine=bentoml`, `framework=torchvision`, and lineage tags.
2. **Given** the registered version promoted to `@serving`, **When** 009's BentoML vision service loads it,
   **Then** it serves predictions over the **new** class labels (the `categories` travel in the checkpoint).
3. **Given** a model resident in GPU serving, **When** a vision fine-tune is requested, **Then** it is refused
   with 409 (one-tenant lease), and a started fine-tune blocks serving from loading until it finishes/frees.

---

### User Story 2 — Embeddings fine-tune (Priority: P2, medium)

The operator fine-tunes a sentence-transformers embedding model with **contrastive / triplet** training on a
pinned pairs/triplets dataset, producing a fine-tuned ST model directory, uploading it to MinIO, and
registering an MLflow version tagged `task=embeddings`, `serving_engine=<009 embeddings engine>`, with lineage
— so 009's embeddings server can load it. The **default** loss is sentence-transformers contrastive with
**MultipleNegativesRankingLoss** (in-batch negatives) over a few epochs (a conservative VRAM-fitting default);
loss/epochs/batch are **exposed on the Runs form** (configurable) and left for **012 HPO** to sweep — no exact
numerics pinned here.

**Why this priority**: Embeddings sit between vision and ASR in difficulty — `sentence-transformers` provides a
high-level `fit`/loss-based trainer (or the HF `Trainer` path), it stays in PyTorch with **no format
conversion** (the served artifact is the ST model directory), and it loads weights+grads+optimizer as a full
GPU lease tenant. P2 because it needs the pairs/triplets dataset shape and a slightly heavier model than the
vision head.

**Independent Test**: With the GPU free, POST an embeddings fine-tune (dataset of pairs/triplets, base ST
model, loss=contrastive|triplet); it runs as a lease tenant, logs the contrastive loss + an eval metric
(e.g. cosine-similarity spread or a small retrieval score) to MLflow, writes a fine-tuned ST model dir to
MinIO, and registers a version tagged `task=embeddings` + `serving_engine` + lineage; promoting it lets 009's
embeddings service return vectors from the fine-tuned model.

**Acceptance Scenarios**:

1. **Given** a pinned pairs/triplets dataset and a base ST model, **When** a contrastive/triplet fine-tune
   runs, **Then** it completes on the GPU, logs the training loss + an eval metric to MLflow, and registers an
   ST-model-dir version tagged `task=embeddings`, `framework=sentence-transformers`, `serving_engine=…`, and
   lineage.
2. **Given** the registered version promoted to `@serving`, **When** 009's embeddings service loads it, **Then**
   it returns embeddings from the fine-tuned model with the same vector contract as the base.

---

### User Story 3 — ASR (Whisper) fine-tune + HF → ggml conversion (Priority: P3, hardest)

The operator fine-tunes a HF `transformers` **Whisper** model (PyTorch, the heaviest of the three) on a pinned
audio+transcript dataset, then **converts the fine-tuned HF model to ggml** for whisper.cpp serving
— the one genuinely **new** piece of tooling (mirroring the existing LoRA → GGUF converter) — uploads the
`ggml-*.bin` to MinIO, and registers an MLflow version tagged `task=asr`, `serving_engine=whisper.cpp`,
`format=ggml`, with lineage. The **default** training approach is **Whisper-small + LoRA (PEFT)** with a low LR,
warmup, and grad-accum sized to fit `VRAM_GB` (a conservative default); these are **exposed on the Runs form**
(configurable) and left for **012 HPO** to sweep — no exact numerics pinned. The conversion uses whisper.cpp's
**`convert-h5-to-ggml`** (the HF route), quantized **q8_0**; if LoRA is used, the adapter is **merged into the
base HF model before** the HF → ggml conversion.

**Why this priority**: ASR is the **hardest** modality — Whisper fine-tuning is a heavier PyTorch job (encoder
+ decoder, feature extraction, seq2seq loss), and it needs a **new format conversion** (HF → ggml) so
009's whisper.cpp server can load it. The conversion is the increment's signature new tool. P3 because it
carries the most VRAM pressure and the only new toolchain dependency.

**Independent Test**: With the GPU free, POST an ASR fine-tune (dataset of audio+transcripts, base Whisper
size); it runs as a lease tenant, logs loss + a WER-style eval metric to MLflow, **converts** the fine-tuned
HF model → ggml (merging any LoRA adapter first), uploads `ggml-*.bin` to MinIO, and registers a version tagged `task=asr`,
`serving_engine=whisper.cpp`, `format=ggml`, lineage; promoting it lets 009's whisper.cpp service transcribe
with the fine-tuned model.

**Acceptance Scenarios**:

1. **Given** a pinned audio+transcript dataset and a base Whisper-small size, **When** an ASR fine-tune runs
   (default Whisper-small + LoRA), **Then** it completes on the GPU within the VRAM budget, logs loss + a
   WER-style metric to MLflow, and produces a fine-tuned HF model (LoRA adapter).
2. **Given** the fine-tuned HF model (LoRA adapter merged into the base first, if used), **When** the
   HF → ggml converter (`convert-h5-to-ggml`, q8_0) runs, **Then** it writes a valid
   `ggml-*.bin`, uploads it to MinIO, and registers a version tagged `task=asr`, `serving_engine=whisper.cpp`,
   `format=ggml`, lineage.
3. **Given** the registered version promoted to `@serving`, **When** 009's whisper.cpp service loads it, **Then**
   it transcribes audio with the fine-tuned model.

---

### User Story 4 — Lineage & adapter-chaining (trained-on-top-of vN) (Priority: P2)

Every fine-tune records **lineage** in MLflow so a version reads as "trained-on-top-of `<base or vN>`", and a
fine-tune MAY **resume from a prior registered version** (chaining): load the previous adapter/checkpoint as a
trainable starting point (mirroring DIMER's `PeftModel.from_pretrained(…, is_trainable=True)`) instead of the
stock base, recording the parent version in the tags.

**Why this priority**: Lineage is the reproducibility backbone (Principle VI) and the precondition for iterative
fine-tuning; chaining turns the registry into a genealogy, not a flat list. P2 because it threads through all
three modalities and is required before any later **gated promotion** (011) can reason about "newer than the
serving version."

**Independent Test**: Run a fine-tune from a stock base — the version carries `base_model` + `lineage=base`.
Run a second fine-tune **resuming from** the first registered version — the new version carries
`parent_version=<v1>` + `lineage=chained`, MLflow shows the parent→child relationship, and the chained model
trains (loss moves) from the prior weights, not the stock base.

**Acceptance Scenarios**:

1. **Given** a fine-tune from a stock base, **When** it registers, **Then** the version's tags record
   `base_model`, `dataset_name`/`dataset_version`, and `lineage=base`.
2. **Given** a prior registered version, **When** a fine-tune resumes from it (`is_trainable` load), **Then** the
   new version records `parent_version` + `lineage=chained` and the MLflow run links to the parent run, and the
   chained training starts from the prior weights (verifiable by the initial loss).

---

### Edge Cases

- **Lease contention (Principle II)**: a fine-tune is the heaviest tenant — it MUST refuse to start (409) while
  a model is resident in serving, and serving MUST refuse to load while a fine-tune runs, exactly like the LLM
  LoRA flow (`_serving_resident()` check + the supervisor mutex). One tenant at a time; the whole GPU.
- **VRAM budget**: full fine-tunes load weights + grads + optimizer; each modality's defaults MUST fit
  `VRAM_GB` (small base sizes, batch size, optional grad-accumulation / grad-checkpointing) — a config that
  OOMs is a violation, not a tuning detail. Failed/OOM runs free the GPU and register **no** partial version
  (mirrors `trainer.py`'s failure path).
- **ggml conversion failure**: a non-zero converter exit (or missing `ggml-*.bin`) MUST fail the ASR run with a
  captured stderr tail and register **no** version (mirrors `convert_to_gguf`).
- **Dataset shape mismatch**: each modality validates its dataset shape up front (vision = labeled images;
  embeddings = pairs/triplets; ASR = audio+transcript) and errors clearly if the pinned version doesn't match.
- **Chaining a wrong-modality parent**: resuming from a parent whose `task` differs from the requested fine-tune
  MUST be rejected before training (no cross-modality chaining).
- **No GPU**: if `nvidia-smi` is unavailable, the heavier fine-tunes (embeddings/ASR) may CPU-fallback only for
  a tiny smoke; the GPU is the supported path (Gate Zero).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-088**: The native trainer (`training/trainer.py` + new `training/flows/`) MUST gain a **vision
  transfer-learning** fine-tune flow: load a torchvision backbone, **freeze the backbone**, **swap the
  classifier head** to the dataset's class count, fine-tune (optionally a low-LR unfreeze pass), and write a
  `model.pt` carrying the new `state_dict` + `categories` (the shape 009's BentoML service loads).
- **FR-089**: The vision flow MUST register a new MLflow model version with tags `task=image-classification`,
  `serving_engine=bentoml`, `framework=torchvision`, `arch=<backbone>`, plus the dataset + lineage tags — so the
  009 vision service can package it from the registry/object store (mirroring `seed_vision_model.py`).
- **FR-090**: The trainer MUST gain a **sentence-transformers embeddings** fine-tune flow using
  **contrastive/triplet** training on a pinned pairs/triplets dataset, writing a fine-tuned ST model directory
  to MinIO.
- **FR-091**: The embeddings flow MUST register a version tagged `task=embeddings`,
  `framework=sentence-transformers`, `serving_engine=<009 embeddings engine>`, plus dataset + lineage tags — so
  the 009 embeddings service loads it.
- **FR-092**: The trainer MUST gain a **HF `transformers` Whisper** ASR fine-tune flow (PyTorch seq2seq) on a
  pinned audio+transcript dataset, logging loss + a WER-style eval metric to MLflow.
- **FR-093**: The ASR flow MUST **convert the fine-tuned HF model to ggml** (`ggml-*.bin`) for whisper.cpp
  serving via whisper.cpp's **`convert-h5-to-ggml`** (the HF route), quantized **q8_0** — a new converter
  mirroring the LoRA → GGUF tool. If the ASR fine-tune used LoRA, the flow MUST **merge the adapter into the
  base HF model before** converting. It MUST fail the run with a captured stderr tail (and register **no**
  version) on a non-zero exit or missing output.
- **FR-094**: The ASR flow MUST register a version tagged `task=asr`, `serving_engine=whisper.cpp`,
  `format=ggml`, plus dataset + lineage tags — so the 009 whisper.cpp service loads it.
- **FR-095**: Every fine-tune flow (vision/embeddings/ASR **and** the existing LLM LoRA) MUST record **lineage**
  tags: `base_model`/base version, `dataset_name`/`dataset_version`, and a `lineage` marker (`base` vs
  `chained`); a chained run MUST also record `parent_version` and link to the parent MLflow run.
- **FR-096**: Any fine-tune flow MAY **resume from a prior registered version** (adapter/checkpoint chaining):
  load the parent artifact as a **trainable** starting point (the `PeftModel.from_pretrained(…,
  is_trainable=True)` pattern for adapter modalities; load the prior `state_dict`/checkpoint for full-weight
  modalities) instead of the stock base, and MUST reject a parent whose `task` differs from the requested
  modality.
- **FR-097**: Each fine-tune MUST run as a **single GPU lease tenant** (Principle II / v1.4.0): it MUST refuse
  to start (409) while a model is resident in serving (`_serving_resident()`), serving MUST refuse to load while
  it runs, and a failed/OOM run MUST free the GPU (`torch.cuda.empty_cache()`) and register **no** partial
  version — reusing the existing trainer-daemon mutex (`trainer.py`), not a new lock.
- **FR-098**: Each modality's training defaults MUST be **conservative VRAM-fitting defaults** (small base,
  batch size, optional grad-accumulation / grad-checkpointing) — vision = freeze-backbone / train-head, small
  LR, few epochs; embeddings = contrastive **MultipleNegativesRankingLoss**, few epochs; ASR = **Whisper-small
  + LoRA**, low LR + warmup + grad-accum sized to `VRAM_GB`. These defaults MUST be **configurable via the Runs
  form** (as the LLM trainer is today) and remain **HPO-tunable (012)** — 010 does **not** hard-pin exact
  LR/epochs/batch. The flows MUST surface the chosen hyperparameters as MLflow params (reproducibility,
  Principle VI). The trainer daemon's `/train` request MUST carry a **modality selector** so one daemon
  dispatches all four flows.
- **FR-099**: 010 MUST register fine-tuned versions **without** auto-promoting them; promotion stays
  operator-driven via the `@serving` alias (as since 004). **Gated/automated promotion is increment 011** and is
  out of scope here. The frozen GPU/FT stack (torch/torchvision/transformers/peft/accelerate/datasets) MUST NOT
  be upgraded; 010 adds code paths, not version bumps.

### Key Entities *(include if feature involves data)*

- **ModalityFineTune**: a single GPU-lease fine-tune run of a given modality (vision | embeddings | asr | llm),
  producing one registered, servable MLflow version with its serving tags.
- **ServableArtifact**: the per-modality output shape — vision `model.pt` (state_dict + categories), embeddings
  ST model directory, ASR `ggml-*.bin` — each content-addressed on the MinIO `models` bucket, the form its 009
  server loads.
- **Lineage**: the MLflow tag/run-link genealogy (`base` vs `chained`, `parent_version`, dataset version) that
  makes "trained-on-top-of vN" explicit and reproducible.
- **GpuLeaseTenant**: the fine-tune as the **heaviest** single-tenant holder of the GPU (weights + grads +
  optimizer) — mutually exclusive with all serving (Principle II / v1.4.0).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-057**: A vision transfer-learning fine-tune (frozen backbone, swapped head) completes on the GPU, logs
  metrics, writes a `model.pt` (state_dict + categories), and registers a version tagged
  `task=image-classification` / `serving_engine=bentoml` / lineage that 009's vision service serves over the new
  labels.
- **SC-058**: An embeddings contrastive/triplet fine-tune completes on the GPU, logs loss + an eval metric,
  writes a fine-tuned ST model dir, and registers a `task=embeddings` version that 009's embeddings service
  loads.
- **SC-059**: A Whisper-small + LoRA ASR fine-tune completes within `VRAM_GB`, logs loss + a WER-style metric,
  **merges the adapter + converts** HF → ggml (`convert-h5-to-ggml`, q8_0), and registers a `task=asr` /
  `serving_engine=whisper.cpp` / `format=ggml` version that 009's whisper.cpp service transcribes with.
- **SC-060**: Lineage is recorded for every fine-tune (`base_model`/`dataset_version`/`lineage`); a chained
  fine-tune records `parent_version` + `lineage=chained` and links to the parent run, and trains from the prior
  weights (initial loss reflects the warm start).
- **SC-061**: The single-GPU lease holds for every modality — a fine-tune requested while a model is resident in
  serving returns 409, a running fine-tune blocks serving from loading, and a failed/OOM run frees the GPU and
  registers no partial version.
- **SC-062**: Every modality's default config fits `VRAM_GB` on the target machine, and the chosen
  hyperparameters appear as MLflow params (reproducible from the recorded config).
- **SC-063**: No regression and no version churn — the existing LLM LoRA flow, the 009 serving paths, and the
  full prior suite pass unchanged, with the frozen GPU/FT stack (torch/torchvision/transformers/peft/accelerate/
  datasets) untouched.

## Assumptions

- **009 served these modalities first** — 010 depends on 009 having stood up the vision/embeddings/ASR
  **serving** paths (BentoML vision already exists at 001; embeddings + whisper.cpp arrive in 009). 010
  fine-tunes *into* those servers' load contracts (`model.pt`+categories, ST dir, `ggml-*.bin`).
- **Deps are mostly already present** — `transformers`/`peft`/`accelerate`/`datasets`/`torchvision`/
  `sentence-transformers` live in `~/mlops-train`; the only genuinely new tooling is the Whisper HF → ggml
  converter (whisper.cpp's `convert-h5-to-ggml`, q8_0; mirroring `convert_to_gguf`). No new heavy deps, no
  GPU-stack bump.
- **The trainer daemon stays single-tenant** — the existing `trainer.py` one-run-at-a-time lock +
  `_serving_resident()` mutex already enforces Principle II; 010 dispatches the new flows behind it via a
  modality selector, not a new daemon or lock.
- **Promotion stays manual** — 010 registers tagged, servable versions; the operator promotes via `@serving`.
  Automated/gated promotion is 011.
- **Reference templates are study-only** — the NAIRA-SEU `training-worker-{llm,audio,yolo}` repos are read for
  shape, not copied; the platform's own LoRA flow is the canonical pattern.

## Non-Goals

- **Gated / automated promotion** of fine-tuned versions to `@serving` — increment **011** (FR-099).
- **Upgrading the frozen GPU/FT stack** (torch/torchvision/transformers/peft/accelerate/datasets) — 010 adds
  code paths only (FR-099 / SC-063); a stack move is its own increment with GPU re-validation.
- **Per-modality drift → retrain auto-fan-out** — having the 001 drift trigger automatically launch vision/
  embeddings/ASR retraining is **out of 010 scope**: it needs per-modality drift detection (image/audio/
  embedding drift), a monitoring concern **deferred to 013 (quality monitoring) / a follow-on**. 010 delivers
  the fine-tune **capability** (run manually/triggered via the Runs flow, registering a servable version) only.
- **A new serving service or new always-on process** — 010 is trainer-only; the servers are 009's.
- **Multi-GPU or concurrent fine-tunes** — one lease tenant at a time (Principle II), unchanged.

## Grilled decisions (2026-06-28)

- **(a) Per-modality hyperparams = sensible defaults, configurable, HPO-tunable — not hard-pinned.** Each
  modality ships a conservative VRAM-fitting **default** that is exposed on the Runs form (like the LLM trainer
  today) and left for **012 HPO** to sweep; 010 does **not** pin exact LR/epochs/batch. Defaults: **vision** =
  freeze the backbone, train the classifier head (transfer learning), small LR, few epochs; **embeddings** =
  sentence-transformers contrastive with **MultipleNegativesRankingLoss** (in-batch negatives), few epochs;
  **ASR** = **Whisper-small + LoRA (PEFT)**, low LR + warmup + grad-accum sized to fit `VRAM_GB`. (FR-088,
  FR-090, FR-092, FR-098 / SC-062)
- **(b) Drift → retrain per-modality = deferred (out of 010 scope).** 010 delivers the fine-tune **capability**
  (run manually/triggered via the Runs flow, registering a servable version). The auto-retrain fan-out (drift on
  vision/ASR/embeddings → launch their retraining) needs per-modality drift detection (image/audio/embedding
  drift) — a monitoring concern folded into **013 (quality monitoring) / a follow-on**. See Non-Goals.
- **(c) Whisper HF → ggml toolchain = `convert-h5-to-ggml` (HF route), q8_0.** The fine-tune output is
  HF-transformers format, so convert with whisper.cpp's **`convert-h5-to-ggml`** (HF route), quantized **q8_0**
  (Whisper base/small are tiny — q8_0 balances size/accuracy; mirrors the LLM LoRA → GGUF pattern). **Not** the
  OpenAI `.pt` `convert-pt-to-ggml` route. If ASR uses LoRA (per (a)), **merge the adapter into the base HF
  model before** the HF → ggml conversion. (FR-093 / SC-059)
