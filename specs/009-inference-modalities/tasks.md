---
description: "Task list for Inference Modalities & Task-Driven UI (009)"
---

# Tasks: Inference Modalities & Task-Driven UI

**Input**: Design documents from `specs/009-inference-modalities/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened, traced, stack-refreshed
platform (002/004/005/006/007) and **008's single GPU lease**. Adds registry task/engine metadata, three
served modalities (embeddings, ASR, tabular), and a dynamic Infer panel — no new lifecycle stage.

**Tests**: Re-run the full 001–008 integration suite per tier on the target machine before the next. Task
IDs continue the shared space (T154+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **DRAFT — GRILLED (2026-06-28), build-ready.**
> Scope: registry `task`/`serving_engine` metadata + gateway routing + dynamic Infer panel (enabler), then
> **embeddings** (BentoML sentence-transformers, **CPU, off-lease, always-on**), **ASR** (whisper.cpp native
> CUDA daemon under the supervisor, GPU-lease tenant), **tabular** (LightGBM CPU, off-lease, always-on).
> Builds on 008's single GPU lease; **no constitution amendment** (gate-checked vs **v1.4.0**; whisper.cpp
> is a native GPU service already allowed since v1.2.0). GPU/FT torch cu128 stack **FROZEN**. Tasks T154–T179.
>
> **Decided (firm FRs):**
> 1. Tag each model version with **`task`** (text-generation/image-classification/embedding/asr/tabular)
>    + **`serving_engine`** (llama.cpp/bentoml/whisper.cpp) via `create_model_version(tags=…)` /
>    `set_model_version_tag`; gateway routes off `search_model_versions("tags.task='…'")`; LoRA inherits
>    the base's serving path.
> 2. **Embeddings** = BentoML `sentence_transformers` **CPU** service, `embed(texts)->vectors`,
>    `@bentoml.api(batchable=True)` (CPU batching); **CPU-only, off-lease, always-available** (parity with
>    tabular). Embeddings *fine-tuning* still uses the GPU; only serving is CPU.
> 3. **ASR** = **whisper.cpp NEW native CUDA daemon** under the supervisor, mirrors llama.cpp EXACTLY
>    (build-from-source CUDA, load-on-demand, idle-release VRAM, joins the lease); gateway proxies
>    `POST /transcribe`. **The only NEW GPU-lease tenant 009 adds.**
> 4. **Tabular** = **LightGBM** default (single joblib, BentoML **CPU** service, `predict(rows)`),
>    **off-lease, always-available**; **AutoGluon documented as an OPTIONAL upgrade path only** (GBM-
>    constrained, `clone_for_deployment(model='best')`, AG+Python pinned).
> 5. **Placement map**: GPU lease (one at a time) = LLM/vision/ASR/training; CPU always-on, off-lease =
>    embeddings + tabular. Infer tab: `embed` + `predict` always live; `stream`/`classify`/`transcribe`
>    lease-governed.
>
> **Grilled decisions (2026-06-28) — no remaining open forks:**
> 1. **Embeddings = CPU, off-lease, always-available BentoML service** (corrected from GPU-lease tenant) —
>    `serving_engine=bentoml`, `@bentoml.api(batchable=True)` still helps CPU batching. *Why*: CPU-embeddings
>    removes the RAG **embed→LLM** swap thrash — embed (CPU) and the LLM (GPU) never contend.
> 2. **Tabular = CPU, off-lease BentoML** LightGBM service.
> 3. **Serving tier for both CPU modalities = a BentoML service** (NOT in-process MLflow pyfunc in the
>    gateway) — keeps the gateway a thin proxy, consistent with vision.
> 4. **The only NEW GPU-lease tenant is ASR** (whisper.cpp). Embeddings is not a lease tenant.

---

## Phase 0 — Pre-flight (gates everything)

- [ ] **T154** [US1] Confirm `sentence-transformers` + `lightgbm` install clean in the BentoML native
  venv; confirm **whisper.cpp builds with CUDA** on the host (gate zero: `nvidia-smi` succeeds in the GPU
  env); confirm `search_model_versions("tags.task='…'")` resolves against the 3.x MLflow server (007).
  *(No GPU/FT stack movement — torch cu128 frozen.)* (FR-074, FR-079, FR-084)

## Phase 1 — Enabler: registry metadata + routing + dynamic panel (US1, P1) → SC-049/050/051

- [ ] **T155** [US1] `gateway/app/registry.py`: add `task` + `serving_engine` helpers — set at
  `create_model_version(tags=…)`, mutate via `set_model_version_tag`, surface both in `list_versions` /
  `list_models`; tolerate legacy versions with no such tags (treat as unknown task). (FR-074)
- [ ] **T156** [US1] `registry.py` + `routers/infer.py`/`serving.py`: **resolve the serving target by
  task** — `@serving` alias + `search_model_versions("tags.task='…'")` + `serving_engine` tag — replacing
  any hard-coded endpoint→model map. Preserve existing LLM `/infer` behavior exactly. (FR-075)
- [ ] **T157** [US1] LoRA-adapter rule: an adapter registered against a base **inherits the base's
  `serving_engine`** and is served on the base's path (no separate engine wiring). (FR-076)
- [ ] **T158** [US1] Retag/reseed the existing models: serving LLM → `task=text-generation`,
  `serving_engine=llama.cpp` (and re-promote `@serving`); vision → `task=image-classification`,
  `serving_engine=bentoml`. Extend `scripts/seed_vision_model.py`; add `scripts/retag_serving_llm.py`. (FR-086)
- [ ] **T159** [US1] Infer tab: query the registry, discover distinct `task` values, render **one panel
  per task** via a renderer map keyed by task; an unknown/unsupported task → read-only **"no renderer"**
  placeholder (never breaks the tab). Keep `embed` + `predict` always live; gate the lease-governed panels (`stream`/`classify`/`transcribe`) per 008. (FR-077, FR-082)
- [ ] **T160** [P] [US1] Re-validate: `test_registry_tasks` (tags set/read/filter), `test_infer_panels`
  (per-task render + fallback), LoRA-inherits-engine; LLM `/infer` + vision `/vision/classify` unchanged. (SC-049, SC-050, SC-051)

## Phase 2 — Embeddings modality (US2, P2) → SC-052

- [ ] **T161** [US2] `serving/bento/embed_service.py`: BentoML **CPU** service packaging a sentence-transformer
  via MLflow's `sentence_transformers` flavor; `embed(texts: list[str]) -> list[list[float]]` with
  `@bentoml.api(batchable=True)` (CPU batching); lazy-load + idle-release (mirror vision/tabular). Add
  `sentence-transformers` to `serving/bento/requirements.txt`. **CPU-only, off-lease, always-available.**
  (FR-078, FR-083) *(grill RESOLVED 2026-06-28: BentoML CPU service over in-process pyfunc, for thin-gateway consistency with vision)*
- [ ] **T162** [US2] `scripts/seed_embedding_model.py`: seed + register the embedding model with
  `task=embedding`, `serving_engine=bentoml` tags. (FR-086)
- [ ] **T163** [US2] `gateway/app/routers/embed.py` + mount in `main.py`: proxy `POST /embed` to the
  embeddings service (thin proxy, mirrors `vision.py`); per-modality reachability check. (FR-078, FR-085)
- [ ] **T164** [P] [US2] `test_embed`: equal-dimension vectors for a batch; the call **succeeds while a GPU
  tenant holds the lease** (embeddings is CPU/off-lease, always-available — parity with tabular);
  `batchable=True` batches a multi-text request. (SC-052)

## Phase 3 — ASR via whisper.cpp native CUDA daemon (US3, P2) → SC-053

- [ ] **T165** [US3] `serving/whispercpp/build.sh`: build whisper.cpp **from source with CUDA** (mirror
  the llama.cpp build); `serving/whispercpp/run.sh`: load-on-demand server launch. (FR-079, FR-084)
- [ ] **T166** [US3] Register the whisper.cpp daemon under the **supervisor** as a GPU-lease tenant —
  load-on-demand, idle-release VRAM, joins the single lease, supervisor-reported health (mirror the
  llama.cpp daemon exactly). (FR-079, FR-083, FR-085)
- [ ] **T167** [US3] `scripts/seed_asr_model.py` (or fold into reseed): register the ASR model with
  `task=asr`, `serving_engine=whisper.cpp` tags. (FR-086)
- [ ] **T168** [US3] `gateway/app/routers/transcribe.py` + mount in `main.py`: proxy `POST /transcribe`
  (audio → text) to the whisper.cpp daemon; per-modality reachability check. (FR-079, FR-085)
- [ ] **T169** [P] [US3] `test_transcribe`: returns a transcript; the daemon **takes the lease on demand**
  and **idle-releases VRAM** (mirroring llama.cpp); swaps out when another GPU tenant takes the lease. (SC-053)

## Phase 4 — Tabular modality (CPU, off-lease) (US4, P3) → SC-054/055

- [ ] **T170** [US4] `serving/bento/tabular_service.py`: BentoML **CPU** service serving a single
  **LightGBM** joblib artifact; `predict(rows: list[dict])`; lazy-load + idle-release. Add `lightgbm` to
  `serving/bento/requirements.txt`. **CPU-only, off-lease, always-available.** (FR-080, FR-084)
- [ ] **T171** [US4] `scripts/seed_tabular_model.py`: train/seed a small LightGBM model → joblib artifact;
  register with `task=tabular`, `serving_engine=bentoml` tags. (FR-080, FR-086)
- [ ] **T172** [US4] `gateway/app/routers/tabular.py` + mount in `main.py`: proxy `POST /predict` to the
  tabular service; per-modality reachability; the predict panel is **always live** regardless of lease. (FR-080, FR-082, FR-085)
- [ ] **T173** [US4] Document **AutoGluon as the OPTIONAL upgrade path only** (NOT default): GBM-
  constrained hyperparameters, `clone_for_deployment(model='best')` to a single artifact, pin AG + Python
  versions; LightGBM stays the default. (FR-081)
- [ ] **T174** [P] [US4] `test_tabular`: one prediction per input row from the CPU joblib artifact; the
  call **succeeds while a GPU tenant holds the lease** (off-lease / always-available); predict panel live. (SC-054, SC-055)

## Phase 5 — Cross-cutting regression

- [ ] **T175** Confirm the **placement map** holds end-to-end: `embed` + `predict` always live;
  `stream`/`classify`/`transcribe` lease-governed (one GPU tenant at a time); no new VRAM assumption;
  torch cu128 frozen. (FR-082, FR-083, SC-055)
- [ ] **T176** [P] Single-lease swap sweep: drive LLM → vision → ASR in sequence and confirm
  **at most one GPU-resident model** at any instant, each swaps the prior out (008 unchanged); embeddings is
  CPU/off-lease and not part of the sweep. (FR-083, SC-056)
- [ ] **T177** [P] Record the **RAG embed→LLM benefit**: because embeddings serves on CPU off-lease, an
  embed-then-infer flow runs with **no model swap** — embed (CPU) and the LLM (GPU) never contend. (spec Edge Cases)
- [ ] **T178** [P] Full **001–008 keyed no-regression sweep**: every lifecycle, SSE framing, status codes,
  the existing tabs + BFF contract, and the frozen GPU stack unchanged. (SC-056)
- [ ] **T179** Commit the new services/routers/seeds/tests + `serving/bento/requirements.txt` bumps +
  `serving/whispercpp/*` + UI panel renderers; the grilled decisions (embeddings = CPU off-lease BentoML;
  both CPU modalities served via BentoML, not in-process pyfunc; ASR the only new lease tenant) are already
  recorded in the spec/plan/tasks status blocks. (SC-049–SC-056)

---

## Dependencies & Execution Order

- **T154 (pre-flight) gates everything** — never start a modality before its dep installs / whisper.cpp
  CUDA build / `tags.task` filter are confirmed on the target machine.
- **US1 (T155–T160) is the enabler** — embeddings/ASR/tabular all depend on registry routing + the
  per-task panel map; it ships first.
- **US2 (embeddings), US3 (ASR), US4 (tabular)** are independent modality tiers on top of US1, each
  re-validated cumulatively; US4 (tabular, CPU/off-lease) is the lowest-risk and may trail.
- **T175–T179 land last** (need every modality in place).

### Constitution gates (re-check each phase, vs v1.4.0)
- Principle II (single GPU lease): **ASR** is the only new tenant (swap/disable per 008); embeddings +
  tabular are CPU-exempt; **at most one GPU-resident model**; verify no torch-family movement.
- Principle III: whisper.cpp idle-releases (no resident cost until used); LightGBM is a light CPU dep.
- Principle V/VI: new deps are swappable; every modality registered in MLflow with `task`/`serving_engine`
  + exposes health. **No new runtime → no amendment** (whisper.cpp native GPU service allowed since v1.2.0).

## Implementation Strategy

1. **Enabler first** → tag + route off registry metadata + dynamic per-task panel. **Stop and validate.**
2. **Embeddings** → BentoML **CPU** service + `/embed` (off-lease, always-available — parity with tabular).
   (Grill RESOLVED: BentoML CPU service over in-process pyfunc, for thin-gateway consistency with vision.)
3. **ASR** → whisper.cpp CUDA daemon under the supervisor (mirror llama.cpp) + `/transcribe` + lease swap.
4. **Tabular** → LightGBM CPU service + `/predict` (off-lease, always-on) + AutoGluon upgrade doc.
5. Each phase re-runs the relevant 001–008 tests on the target machine; never regress; never move the
   frozen torch cu128 stack; at most one GPU-resident model at any instant.

## Out of Scope (recorded)
- **Multi-model-in-VRAM / GPU co-residency**: the single 008 lease is unchanged (FR-083).
- **AutoGluon as the default**: LightGBM is the default; AutoGluon is an optional, pinned, GBM-constrained
  upgrade path only (FR-081).
- **Non-CUDA / cloud ASR**: whisper.cpp native CUDA only (FR-079).
- **A RAG pipeline / vector store**: 009 serves the embedding *modality*; the retrieval chain is a later
  increment.
- **GPU/FT stack movement** (torch/transformers/…): frozen (Principle II / 007) — a future increment with
  its own GPU re-validation.
- **New lifecycle stage**: all three modalities are serving/inference (existing stage); no amendment.
