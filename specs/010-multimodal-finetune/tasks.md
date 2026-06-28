---
description: "Task list for Multimodal Fine-Tuning — Vision · Embeddings · ASR (010)"
---

# Tasks: Multimodal Fine-Tuning (Vision · Embeddings · ASR)

**Input**: Design documents from `specs/010-multimodal-finetune/`

**Prerequisites**: plan.md (required), spec.md (required); builds on **009** (which served the
vision/embeddings/ASR modalities) and on the LLM LoRA trainer (`training/trainer.py` +
`training/flows/finetune.py`). Extends the trainer only — no new service/runtime; promotion stays manual
(gated promotion is **011**).

**Tests**: Each modality re-validates the **single-GPU lease mutex** (refuse-while-serving-resident, free-on-
failure) + the **009 serving load contract** on the target machine before the next. Task IDs continue the
shared space (T180+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **DRAFT — GRILLED (2026-06-28), build-ready.**
> Scope: **extend the LLM-only trainer to fine-tune vision + embeddings + ASR**, each registering a servable
> MLflow version with serving tags + lineage; each fine-tune is the **heaviest single GPU lease tenant**
> (Principle II / constitution v1.4.0). GPU/FT stack **FROZEN** (code paths only, no version bumps). No
> constitution amendment (training-as-a-lease-tenant already covered by v1.4.0; existing native Python runtime;
> deps mostly present). Tasks T180–T201.
>
> **Firm decisions:**
> 1. **Phase by difficulty**: US1 vision (easiest, torchvision transfer-learning) → US2 embeddings (medium,
>    sentence-transformers contrastive/triplet) → US3 ASR (hardest, HF Whisper-small+LoRA + new HF→ggml
>    converter) → US4 lineage/chaining (threads all flows).
> 2. **Reuse the existing trainer mutex** (`trainer.py` `_lock` + `_serving_resident()` + free-on-failure) — a
>    fine-tune is the heaviest lease tenant; NO new lease/lock.
> 3. **Fine-tune into the 009 serving load contracts**: vision `model.pt`(`{state_dict, categories}`),
>    embeddings ST model **dir**, ASR `ggml-*.bin` — a promoted version is servable with no server change.
> 4. **New tooling = the Whisper HF→ggml converter** (`convert-h5-to-ggml`, q8_0; mirrors `convert_to_gguf`);
>    everything else is new trainer code on **already-present** deps (transformers/peft/accelerate/datasets/
>    torchvision/sentence-transformers).
> 5. **GPU/FT stack FROZEN** (non-negotiable): torch/torchvision/transformers/peft/accelerate/datasets
>    unchanged — add code, not versions.
> 6. **Promotion stays manual** (`@serving` alias, as since 004); **gated/automated promotion is 011**.
>
> **Grilled decisions (2026-06-28):**
> - **a) Per-modality hyperparams = sensible defaults, configurable, HPO-tunable** — conservative VRAM-fitting
>   defaults (vision = freeze-backbone / train-head, small LR, few epochs; embeddings = contrastive
>   **MultipleNegativesRankingLoss**, few epochs; ASR = **Whisper-small + LoRA**, low LR + warmup + grad-accum
>   to fit `VRAM_GB`), all **exposed on the Runs form** and left for **012 HPO** to sweep; no exact
>   LR/epochs/batch pinned.
> - **b) Drift → retrain per-modality = DEFERRED (out of 010 scope)** — 010 ships the fine-tune capability via
>   the Runs flow; auto-retrain fan-out on per-modality drift folds into **013 (quality monitoring) / a
>   follow-on**.
> - **c) Whisper HF→ggml = `convert-h5-to-ggml` (HF route), q8_0** — the fine-tune output is HF-transformers;
>   **not** the OpenAI `.pt` `convert-pt-to-ggml` route. If ASR uses LoRA, **merge the adapter into the base HF
>   model before** converting.

---

## Phase 0 — Pre-flight (gates everything)

- [ ] **T180** [US1] Confirm `sentence-transformers` + the Whisper fine-tune deps resolve in `~/mlops-train`
  **without** moving the frozen torch/cu128 family (`pip install --dry-run` / check resolved torch unchanged).
  Record any newly-pinned native dep for `scripts/native_env.lock`. (FR-090, FR-099)
- [ ] **T181** [P] [US3] Smoke whisper.cpp's **`convert-h5-to-ggml`** (HF route, quantized **q8_0**) on a
  stock Whisper-small HF checkpoint → a loadable `ggml-*.bin`. (Decided: HF route, not the OpenAI `.pt`
  `convert-pt-to-ggml`; q8_0.) (FR-093)
- [ ] **T182** [P] Confirm the **009 serving load contracts** are exactly: vision `model.pt`
  `{state_dict, categories}` (BentoML), embeddings ST model **dir**, ASR `ggml-*.bin` (whisper.cpp) — so the
  flows target the right artifact shape. (FR-089, FR-091, FR-094)

## Phase 1 — Vision transfer-learning (US1, P1) → SC-057 + lease half of SC-061

- [ ] **T183** [US1] `training/flows/vision_finetune.py`: pull a pinned image-classification dataset version
  from MinIO; load a torchvision backbone; **default** = **freeze the backbone**, **swap the classifier head**
  to the dataset's class count, small LR, few epochs (optional low-LR unfreeze pass); surface these as
  **Runs-form params** (configurable, HPO-tunable by 012), not hard-pinned; free the GPU promptly. (FR-088,
  FR-098)
- [ ] **T184** [US1] Write the output `model.pt` carrying `{state_dict, categories}` (the 009 BentoML load
  shape); upload to MinIO `models` bucket (content-addressed); register an MLflow version tagged
  `task=image-classification`, `serving_engine=bentoml`, `framework=torchvision`, `arch=<backbone>` + dataset
  tags (mirrors `scripts/seed_vision_model.py`). (FR-089)
- [ ] **T185** [US1] `training/trainer.py`: add a **`modality` selector** to `/train` and dispatch to the right
  flow (vision now; embeddings/asr later); keep the one-run `_lock` + `_serving_resident()` mutex + free-on-
  failure + no-partial-version path **unchanged**. (FR-097, FR-098)
- [ ] **T186** [P] [US1] `tests/test_vision_finetune`: with the GPU free, a vision fine-tune completes, logs
  metrics, registers the tagged version; promoting to `@serving` makes 009's vision service classify the
  **new** labels; a fine-tune requested while a model is serving-resident returns **409**. (SC-057, SC-061)

## Phase 2 — Embeddings fine-tune (US2, P2) → SC-058

- [ ] **T187** [US2] `training/flows/embeddings_finetune.py`: pull a pinned **pairs/triplets** dataset; load a
  base sentence-transformers model; **default** = contrastive **MultipleNegativesRankingLoss** (in-batch
  negatives), few epochs (ST `fit`/loss or HF `Trainer` path) as a full GPU lease tenant; surface loss/epochs/
  batch as **Runs-form params** (configurable, HPO-tunable by 012), not hard-pinned; free the GPU promptly.
  (FR-090, FR-098)
- [ ] **T188** [US2] Save the fine-tuned **ST model directory**; upload to MinIO `models`; register an MLflow
  version tagged `task=embeddings`, `framework=sentence-transformers`, `serving_engine=<009 embeddings engine>`
  + dataset tags. (FR-091)
- [ ] **T189** [US2] Wire the `embeddings` modality into the `trainer.py` dispatch (no new lock). (FR-097)
- [ ] **T190** [P] [US2] `tests/test_embeddings_finetune`: a contrastive/triplet fine-tune completes, logs loss
  + an eval metric (cosine spread / small retrieval score), registers the tagged version; promoting it lets
  009's embeddings service return fine-tuned vectors with the same contract. (SC-058)

## Phase 3 — ASR (Whisper) fine-tune + HF→ggml (US3, P3) → SC-059

- [ ] **T191** [US3] `training/flows/asr_finetune.py`: pull a pinned **audio+transcript** dataset; HF
  `transformers` **Whisper-small + LoRA (PEFT)** seq2seq fine-tune (feature extractor + tokenizer + seq2seq
  loss) within `VRAM_GB` — **default** low LR + warmup + grad-accum, **exposed on the Runs form** (configurable,
  HPO-tunable by 012), not hard-pinned; log loss + a **WER-style** eval metric; free the GPU promptly. (FR-092,
  FR-098)
- [ ] **T192** [US3] `training/tools/convert_whisper_to_ggml.py` (**the new tool**): **merge the LoRA adapter
  into the base HF model**, then convert via whisper.cpp's **`convert-h5-to-ggml`** (HF route, **q8_0**) →
  `ggml-*.bin`; **fail the run** with a captured stderr tail (and register **no** version) on non-zero exit or
  missing output (mirrors `convert_to_gguf`). (FR-093)
- [ ] **T193** [US3] Upload `ggml-*.bin` to MinIO `models`; register an MLflow version tagged `task=asr`,
  `serving_engine=whisper.cpp`, `format=ggml` + dataset tags; wire the `asr` modality into the `trainer.py`
  dispatch. (FR-094, FR-097)
- [ ] **T194** [P] [US3] `tests/test_asr_finetune`: a Whisper-small+LoRA fine-tune completes within `VRAM_GB`,
  logs loss + WER-style metric, **merges the adapter + converts** HF→ggml (`convert-h5-to-ggml`, q8_0),
  registers the tagged version; promoting it lets 009's whisper.cpp service transcribe with the fine-tuned
  model; converter non-zero exit registers **no** version. (SC-059, SC-061)

## Phase 4 — Lineage & adapter-chaining (US4, P2) → SC-060

- [ ] **T195** [US4] `training/flows/lineage.py`: shared helper to stamp lineage tags on every fine-tune
  (`base_model`/base version, `dataset_name`/`dataset_version`, `lineage=base|chained`) and, for chained runs,
  `parent_version` + a link to the parent MLflow run. (FR-095)
- [ ] **T196** [US4] Resume-from-prior-version (**chaining**): load the parent artifact as a **trainable**
  start — `PeftModel.from_pretrained(…, is_trainable=True)` for adapter modalities, prior `state_dict`/
  checkpoint for full-weight modalities — instead of the stock base; **reject** a parent whose `task` differs
  from the requested modality (no cross-modality chaining). (FR-096)
- [ ] **T197** [US4] Adopt `lineage.py` in all four flows (vision/embeddings/asr **and** the existing LLM
  `finetune.py` — minimal change: route its existing tags through the helper). (FR-095)
- [ ] **T198** [P] [US4] `tests/test_lineage_chaining`: a base fine-tune records `lineage=base`; a fine-tune
  resuming from a prior version records `parent_version` + `lineage=chained`, links to the parent run, and
  trains from the prior weights (initial loss reflects the warm start); a wrong-modality parent is rejected.
  (SC-060)

## Phase 5 — Cross-cutting (VRAM-fit, params, no-regression, frozen-stack)

- [ ] **T199** Confirm each modality's **default config fits `VRAM_GB`** on the target machine (small base,
  batch, optional grad-accum / grad-checkpointing) and surfaces the chosen hyperparameters as **MLflow params**
  (reproducible from the recorded config). (SC-062, FR-098)
- [ ] **T200** **Frozen-stack check**: verify no movement in torch/torchvision/transformers/peft/accelerate/
  datasets (`pip freeze` diff vs the validated lock); `scripts/native_env.lock` updated only for the
  newly-added native dep(s). (SC-063, FR-099)
- [ ] **T201** Full no-regression sweep: the existing LLM LoRA flow + the 009 serving paths + the prior suite
  pass unchanged; the lease mutex holds for every modality (409-while-resident, free-on-failure, no partial
  version); commit the new flows/tool/tests + `native_env.lock`. **Promotion stays manual; gated promotion is
  011.** (SC-061, SC-063)

---

## Dependencies & Execution Order

- **T180–T182 (pre-flight) gate everything** — confirm deps resolve without moving the frozen stack, smoke the
  HF→ggml converter (`convert-h5-to-ggml`, q8_0), and lock the 009 load contracts before writing flows.
- **US1 vision (T183–T186)** leads (lowest risk) — it de-risks the lease mutex, registry-tag, and 009-load
  mechanics for the harder modalities.
- **US2 embeddings (T187–T190)** and **US3 ASR (T191–T194)** are independent modality tiers; **US4 lineage
  (T195–T198)** threads through all of them (do it after at least US1 so there's a real chain to test).
- **T199–T201 land last** (need every modality in place).

### Constitution gates (re-check each phase — against v1.4.0 lease model)
- Principle II intact: each fine-tune is the **heaviest single lease tenant** — refuse-while-serving-resident,
  free-on-failure, **one tenant at a time**; reuse the trainer mutex, no new lock.
- Principle IV/VI strengthened: training now spans all served modalities; every fine-tune is tracked with
  lineage/serving tags (reproducible genealogy).
- No new runtime/service → no amendment (existing native Python venv; deps mostly present).
- Frozen GPU/FT stack: add code paths, **never** bump torch/transformers/peft/accelerate/datasets/torchvision.

## Implementation Strategy

1. **Pre-flight** (deps resolve frozen-stack-clean; smoke the ggml converter; lock the 009 contracts).
2. **Vision first** → register + serve new labels behind the lease mutex. **Stop and validate.**
3. **Embeddings → ASR** (+ the new HF→ggml converter, `convert-h5-to-ggml` q8_0), each behind its own test gate + 009 serve check.
4. **Lineage/chaining** across all flows (incl. the existing LLM one).
5. Each phase re-validates the lease + the 009 load contract on the target machine; never regress; never move
   the frozen GPU stack. Promotion stays manual.

## Out of Scope (recorded)
- **Gated / automated promotion** to `@serving` — increment **011** (FR-099).
- **GPU/FT stack upgrade** (torch/torchvision/transformers/peft/accelerate/datasets): frozen — code paths only
  (SC-063); a stack move is its own increment with GPU re-validation.
- **Per-modality drift → retrain auto-fan-out** — DEFERRED to **013 (quality monitoring) / a follow-on**
  (needs per-modality image/audio/embedding drift detection); 010 is the fine-tune capability only, not built
  here.
- **New serving service / always-on process** — trainer-only; servers are 009's.
- **Multi-GPU / concurrent fine-tunes** — one lease tenant at a time (Principle II), unchanged.
