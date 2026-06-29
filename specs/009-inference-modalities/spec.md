# Feature Specification: Inference Modalities & Task-Driven UI

**Feature Branch**: `009-inference-modalities`

**Created**: 2026-06-28

**Status**: **BUILT (2026-06-29) — offline-validated; on-hardware validation PENDING** (was DRAFT —
GRILLED 2026-06-28, build-ready). All five phases implemented; py_compile + tsc + pytest-collect +
the stdlib gpu_lease unit test green, new modality tests skip-clean. Pending on the GPU host: deps
install + whisper.cpp CUDA build, then the keyed no-regression + lease-swap sweep (see tasks.md).

**Grilled decisions (2026-06-28):**
1. **Embeddings = CPU, off-lease, always-available BentoML service** (corrected from GPU-lease tenant).
   Served as a `sentence_transformers` BentoML service with `serving_engine=bentoml` and
   `@bentoml.api(batchable=True)` (still helps CPU batching) — just **CPU/off-lease**, exactly like tabular.
   *Rationale*: CPU-embeddings removes the RAG embed→LLM swap thrash — embed (CPU) and LLM (GPU) never
   contend. (Embeddings *fine-tuning* still uses the GPU; only serving moves to CPU.)
2. **Tabular = CPU, off-lease** BentoML LightGBM service (single joblib artifact).
3. **Serving tier for both CPU modalities = a BentoML service** (NOT in-process MLflow pyfunc in the
   gateway) — keeps the gateway a thin proxy, consistent with vision.
4. **The only NEW GPU-lease tenant 009 adds is ASR** (whisper.cpp native CUDA daemon). Embeddings is not a
   lease tenant. No remaining open forks.

**Kurt's rule**: embeddings + tabular are the ONLY CPU modalities; GPU lease (one tenant at a time) =
LLM, vision, ASR, training.

**Input**: Roadmap pull (2026-06-28) — the platform serves two inference modalities today (LLM
text-generation on llama.cpp, vision image-classification on BentoML) behind a registry whose model
versions carry no machine-readable notion of *what task a model performs* or *which engine serves it*.
009 closes that gap and uses it to add three more served modalities — **embeddings**, **ASR**, and
**tabular** — plus a **task-driven Infer tab** that renders a panel per task from registry metadata.
009 **builds on 008's GPU lease** (Principle II, v1.4.0): the new GPU-resident modality (ASR) shares the
single lease; the new **embeddings** and **tabular** modalities are CPU-only and off-lease (always
available).

> **Scope note**: 009 is an **additive capability increment** — it adds registry task/engine metadata,
> three new served modalities, and a dynamic Infer panel. It introduces no new lifecycle *stage* (all
> three are serving/inference, an existing stage), and **no constitution amendment**: whisper.cpp is a
> native CUDA GPU service already permitted under the v1.2.0 hybrid-GPU amendment; embeddings and
> tabular reuse the existing BentoML serving path on **CPU** (Principle V swappable, Principle III lightweight).
> Requirement IDs continue the shared space (FR-074+, SC-049+, tasks T154+). See plan.md →
> Constitution Check (gated against constitution **v1.4.0** — the 008 GPU-lease amendment).

> **GPU-lease boundary (from 008, NON-NEGOTIABLE)**: exactly **one tenant** holds the single GPU lease
> at any instant — LLM / vision / ASR **OR** training; **CPU-only models (embeddings, tabular) are
> exempt**. The one new GPU modality 009 adds (ASR) JOINS the lease as an ordinary tenant and obeys the
> existing swap/disable behavior; **embeddings and tabular are CPU-only and never touch the lease**. 009
> adds no new VRAM assumption and frozen GPU pins (torch cu128) are unchanged.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Registry task + serving-engine metadata, gateway routing, dynamic Infer panel (Priority: P1)

Every registered MLflow model version is tagged with a **`task`** (text-generation / image-classification
/ embedding / asr / tabular) and a **`serving_engine`** (llama.cpp / bentoml / whisper.cpp). The gateway
**routes inference off this registry metadata** rather than hard-coded endpoint-to-model wiring, and the
Infer tab **renders a panel per `task`** discovered from the registry — so adding a future modality means
registering a model with a `task` tag and dropping in a small renderer, not re-plumbing the gateway/UI.

**Why this priority**: This is the **enabler** — embeddings/ASR/tabular all depend on the gateway being
able to find "the serving model for task X on engine Y" from the registry, and on the UI rendering a
panel keyed by task. It is the load-bearing change; the three modalities are thin once it exists. It
ships first so the others slot in.

**Independent Test**: Register a model version with `tags={task, serving_engine}`; `GET` the registry and
confirm both tags resolve; `search_model_versions("tags.task='text-generation'")` returns it; the gateway
resolves the serving target for a task from the alias + task tag (not a hard-coded map); a LoRA adapter
registered against a base inherits the base's `serving_engine` (serving path). The Infer tab queries the
registry, discovers the available tasks, and renders one panel per task (text-generation panel present
for the seeded LLM).

**Acceptance Scenarios**:

1. **Given** the serving LLM and vision model, **When** each is (re-)registered with `task` +
   `serving_engine` version tags (`create_model_version(tags=…)` / `set_model_version_tag`), **Then**
   both tags are returned by the registry list and `search_model_versions("tags.task='…'")` filters to
   the right versions.
2. **Given** the tagged registry, **When** the gateway serves `/infer` (and the other modality routes),
   **Then** it resolves the serving model/engine for the request's task **from registry metadata** (the
   `@serving` alias + `task`/`serving_engine` tags), with the previous behavior preserved for the LLM.
3. **Given** a LoRA adapter registered against a base model, **When** its tags are read, **Then** it
   **inherits the base's `serving_engine`** (it is served on the base's path), so adapters never need a
   separate engine wiring.
4. **Given** the registry exposes N distinct `task` values, **When** the Infer tab loads, **Then** it
   renders **one panel per task** (dynamic), and an unknown/unsupported task degrades to a read-only
   "no renderer" placeholder rather than breaking the tab.

---

### User Story 2 — Embeddings modality (CPU, off-lease) (Priority: P2)

A sentence-transformer embedding model is served via a **BentoML CPU service** (consistent with vision and
tabular; keeps the gateway a thin proxy), packaged with MLflow's `sentence_transformers` flavor, exposing
`embed(texts: list[str]) -> list[list[float]]` with `@bentoml.api(batchable=True)` (still helps CPU
batching). It is **CPU-only, off the GPU lease, and ALWAYS available** — an `embed` call never contends for
VRAM and never takes the lease, so it succeeds even while a GPU tenant holds the lease (exactly like
tabular). The gateway proxies `POST /embed`.

**Why this priority**: Embeddings are the simplest new modality (no new native engine — reuses the
BentoML path) and unlock RAG-style flows. P2 because it depends on US1's routing/metadata but is lower
risk than the new native ASR daemon. Serving on CPU also removes the RAG embed→LLM swap thrash — embed
(CPU) and LLM (GPU) never contend.

**Independent Test**: Register the embedding model with `task=embedding`, `serving_engine=bentoml`;
`POST /embed` with a list of strings returns a list of equal-length float vectors; the call **succeeds
while a GPU tenant holds the lease** (proving off-lease/always-available); batching of multiple texts in one
call works.

**Acceptance Scenarios**:

1. **Given** a registered `task=embedding` model, **When** `POST /embed {"texts": ["a","b"]}` is called,
   **Then** the response is a list of two equal-dimension float vectors computed on CPU, and the call does
   **not** take the GPU lease.
2. **Given** a GPU tenant (e.g. the LLM) currently holds the lease, **When** `/embed` is called, **Then**
   it still succeeds (embeddings is off-lease, CPU-only) — so a RAG embed→LLM flow runs with **no model
   swap** (embed on CPU and the LLM on GPU never contend).
3. **Given** `@bentoml.api(batchable=True)`, **When** a multi-text request is sent, **Then** the texts are
   embedded in one batched forward pass (throughput, not one-per-call).

---

### User Story 3 — ASR modality via whisper.cpp native CUDA daemon (Priority: P2)

A **whisper.cpp** engine is added as a **NEW native CUDA daemon under the supervisor**, mirroring the
llama.cpp pattern exactly: build-from-source with CUDA, load-on-demand, idle-release VRAM, and **joins
the GPU lease** as a tenant. The gateway proxies `POST /transcribe` (audio in → text out).

**Why this priority**: ASR is the highest-effort new modality (a new native build-from-source CUDA engine
+ supervisor integration + lease participation), so it is specified carefully but sits at P2 alongside
embeddings. It is independent of US2.

**Independent Test**: whisper.cpp builds from source with CUDA on the target host and registers under the
supervisor; a model registered with `task=asr`, `serving_engine=whisper.cpp` resolves; `POST /transcribe`
with an audio clip returns a transcript; the daemon takes the GPU lease on demand and releases VRAM when
idle (mirroring llama.cpp); it swaps out when another tenant takes the lease.

**Acceptance Scenarios**:

1. **Given** whisper.cpp built with CUDA and supervised, **When** `POST /transcribe` is called with an
   audio file, **Then** it returns the transcribed text and the daemon held the GPU lease for the call.
2. **Given** the ASR daemon is idle past its timeout, **When** the watcher runs, **Then** it releases
   VRAM (scale-to-zero), exactly like the llama.cpp daemon.
3. **Given** another GPU tenant (LLM/vision/training) requests the lease, **When** ASR holds
   it, **Then** the single-lease swap applies — at most one model resident (Principle II / 008).

---

### User Story 4 — Tabular modality (CPU, off-lease, always available) (Priority: P3)

A tabular model (default **LightGBM**, a single joblib artifact) is served via a **BentoML CPU service**,
exposing `predict(rows: list[dict]) -> …`. It is **CPU-only, off the GPU lease, and ALWAYS available** —
it never contends for VRAM, so a `predict` call works even while a GPU tenant holds the lease. AutoGluon
is documented as an **optional upgrade path only** (not the default).

**Why this priority**: Tabular rounds out the modality set and is the lowest-risk (CPU, mirrors the vision
BentoML pattern, always-on). P3 — valuable but not blocking.

**Independent Test**: Register a LightGBM model with `task=tabular`, `serving_engine=bentoml`; `POST
/predict` with a list of row dicts returns predictions; the call **succeeds while a GPU tenant holds the
lease** (proving off-lease/always-available); the Infer tab's tabular `predict` panel is live regardless
of lease state.

**Acceptance Scenarios**:

1. **Given** a registered `task=tabular` LightGBM model, **When** `POST /predict {"rows":[{…},{…}]}` is
   called, **Then** it returns one prediction per row from a single joblib artifact on CPU.
2. **Given** a GPU tenant (e.g. the LLM) currently holds the lease, **When** `/predict` is called, **Then**
   it still succeeds (tabular is off-lease, CPU-only) — the Infer tab's tabular panel stays live.
3. **Given** the AutoGluon upgrade path is chosen (optional, not default), **When** documented,
   **Then** the doc constrains hyperparameters to GBM, uses `clone_for_deployment(model='best')`, and
   pins the AutoGluon + Python versions — so it stays a single-artifact, CPU, drop-in replacement.

---

### Edge Cases

- **Placement map (state it explicitly)**: GPU lease, **one tenant at a time** = LLM (text-generation),
  vision (image-classification), ASR (asr), training. **CPU, always-on, off-lease** = embeddings (embed),
  tabular (predict). The Infer tab: `embed` (embeddings) and `predict` (tabular) are **always live**;
  `stream`/`classify`/`transcribe` are **lease-governed** (disabled/swapped per 008).
- **RAG embed→LLM has no swap cost**: because embeddings runs on CPU off-lease, a RAG embed→LLM flow runs
  with **no model swap** — embed (CPU) and the LLM (GPU) never contend. (Recorded as a benefit of the CPU
  placement, not a cost.)
- **Untagged legacy versions**: a version registered before 009 has no `task`/`serving_engine` tag; the
  registry list MUST tolerate the absence (treat as unknown task), and the Infer tab degrades that to a
  "no renderer" placeholder rather than erroring (FR-074, FR-079).
- **Unknown task in the UI**: a task the UI has no renderer for shows a read-only placeholder; adding a
  renderer is the only change needed to support it (FR-074).
- **whisper.cpp build failure**: if the CUDA build fails on the host, the supervisor reports the ASR
  daemon unhealthy (mirrors llama.cpp); the rest of the platform (and other lease tenants) stays up.
- **No-regression**: the full 001–008 suite passes; the one-model-in-VRAM single-lease behavior, SSE
  framing, every status code, and the existing five tabs are unchanged (SC-056).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-074**: Each registered MLflow model version MUST carry a **`task`** version tag (one of
  `text-generation` / `image-classification` / `embedding` / `asr` / `tabular`) and a **`serving_engine`**
  version tag (`llama.cpp` / `bentoml` / `whisper.cpp`), set at `create_model_version(tags=…)` and
  mutable via `set_model_version_tag`. The registry list/read MUST surface both, and versions registered
  before 009 (no such tags) MUST be tolerated (treated as an unknown task).
- **FR-075**: The gateway MUST **route inference off registry metadata** — it resolves the serving model
  and engine for a request's task from the `@serving` alias plus the `task`/`serving_engine` tags
  (`search_model_versions("tags.task='…'")`), rather than a hard-coded endpoint→model map. Existing LLM
  `/infer` behavior MUST be preserved.
- **FR-076**: A **LoRA adapter** registered against a base model MUST **inherit the base's
  `serving_engine`** (and serving path); adapters are not given a separate engine wiring.
- **FR-077**: The Infer tab MUST **render one panel per `task`** discovered from the registry (dynamic).
  Adding a modality = register a model with a `task` tag + add a small renderer. An unknown/unsupported
  task MUST degrade to a read-only "no renderer" placeholder, never break the tab.
- **FR-078**: An **embeddings** modality MUST be served via a **BentoML CPU service** (NEW dep
  `sentence-transformers`), packaged with MLflow's `sentence_transformers` flavor, exposing
  `embed(texts: list[str]) -> list[list[float]]` with `@bentoml.api(batchable=True)` (CPU batching). It MUST
  be **CPU-only, off the GPU lease, and always available** — succeeding even while a GPU tenant holds the
  lease. The gateway MUST proxy `POST /embed`.
- **FR-079**: An **ASR** modality MUST be served by **whisper.cpp as a NEW native CUDA daemon under the
  supervisor**, mirroring the llama.cpp pattern exactly: build-from-source with CUDA, load-on-demand,
  idle-release VRAM, **joins the GPU lease**. The gateway MUST proxy `POST /transcribe` (audio → text).
- **FR-080**: A **tabular** modality MUST be served via a **BentoML CPU service** (NEW dep **LightGBM**,
  default; a single joblib artifact), exposing `predict(rows: list[dict])`. It MUST be **CPU-only,
  off the GPU lease, and always available** — succeeding even while a GPU tenant holds the lease.
- **FR-081**: **AutoGluon** MUST be documented as an **optional upgrade path only** (NOT the default):
  constrain hyperparameters to GBM, deploy via `clone_for_deployment(model='best')` to a single artifact,
  and pin the AutoGluon + Python versions. The default tabular engine remains LightGBM.
- **FR-082**: The placement map MUST hold and be documented: GPU lease (one tenant at a time) = LLM,
  vision, ASR, training; CPU always-on / off-lease = embeddings, tabular. The Infer tab MUST keep
  `embed` and `predict` always live and gate `stream`/`classify`/`transcribe` on the lease (per 008).
- **FR-083**: All new modalities MUST honor the 008 **single GPU lease** — at most one GPU-resident model
  at any instant; ASR participates in the existing swap/disable behavior; embeddings and tabular are exempt
  (CPU-only). No new VRAM assumption is introduced and the frozen torch cu128 stack is unchanged.
- **FR-084**: The new deps (`sentence-transformers`, `whisper.cpp`, `LightGBM`) MUST be **additive and
  swappable** (Principle V) and stay within the lightweight footprint (Principle III): whisper.cpp is a
  native build (no resident cost until used, idle-release), LightGBM is a light CPU dep, embeddings reuse
  the existing BentoML serving runtime.
- **FR-085**: Each new modality MUST expose **health/readiness** consistent with the existing services
  (the BentoML `/readyz` pattern for embeddings/tabular; the supervisor-reported health for the whisper.cpp
  daemon, mirroring llama.cpp), and the gateway MUST surface a per-modality reachability check.
- **FR-086**: Re-seeding/registration scripts MUST register each modality's model with its `task` +
  `serving_engine` tags (extending the existing `seed_vision_model.py` pattern), so a clean bring-up
  resolves all panels; legacy untagged versions remain valid.
- **FR-087**: Each modality MUST be validated against the full 001–008 integration suite on the target
  machine before the next; nothing may regress any lifecycle, the single-lease VRAM behavior, SSE framing,
  the existing tabs, or the frozen GPU stack.

### Key Entities *(include if feature involves data)*

- **TaskTaggedVersion**: an MLflow model version carrying `task` + `serving_engine` version tags; the unit
  the gateway routes on and the Infer tab renders a panel from.
- **ServingEngine**: one of `llama.cpp` / `bentoml` / `whisper.cpp` — the engine that serves a task; a
  LoRA adapter inherits its base's engine.
- **GpuLeaseTenant**: a GPU-resident modality (LLM / vision / ASR) or training — at most one holds the
  single 008 lease at a time; **embeddings and tabular are not tenants** (CPU, off-lease).
- **CpuModality**: a CPU-only, off-lease, always-available service — **embeddings** (a `sentence_transformers`
  BentoML service) and **tabular** (a single joblib artifact, LightGBM default; AutoGluon optional clone);
  neither takes the GPU lease.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-049**: Every served model version exposes a `task` + `serving_engine` tag;
  `search_model_versions("tags.task='…'")` filters correctly and the gateway resolves the serving target
  for each task from registry metadata (no hard-coded endpoint→model map).
- **SC-050**: The Infer tab renders one panel per registry task (dynamic); an unknown task shows a
  read-only "no renderer" placeholder without breaking the tab; a new modality needs only a registration
  + a small renderer.
- **SC-051**: A LoRA adapter registered against a base resolves with the **base's** `serving_engine` and
  is served on the base's path.
- **SC-052**: `POST /embed` returns equal-dimension float vectors for a batch of texts, the call **succeeds
  while a GPU tenant holds the lease** (embeddings is off-lease / CPU-only), and `@bentoml.api(batchable=True)`
  batches a multi-text request.
- **SC-053**: whisper.cpp builds with CUDA under the supervisor; `POST /transcribe` returns a transcript;
  the daemon takes the lease on demand and idle-releases VRAM (mirroring llama.cpp).
- **SC-054**: `POST /predict` returns one prediction per input row from a single CPU joblib artifact, and
  the call **succeeds while a GPU tenant holds the lease** (tabular is off-lease / always available).
- **SC-055**: The placement map holds — `embed` and `predict` are always live; `stream`/`classify`/`transcribe`
  are lease-governed (one GPU tenant at a time); no new VRAM assumption; frozen torch cu128 unchanged.
- **SC-056**: No regression — the full 001–008 suite passes; the single-lease VRAM mutex, SSE framing,
  every status code, and the existing tabs/BFF contract are unchanged.

## Assumptions

- **008's GPU lease is the substrate** — 009 adds one tenant (ASR) to the existing single lease and two
  exempt CPU modalities (embeddings, tabular); it does not change the lease mechanism or add a VRAM assumption.
- **Registry metadata is the routing source of truth** — the gateway and UI read `task`/`serving_engine`
  from MLflow version tags; this is the modern alias/tag path already in use (registry.py uses aliases +
  version tags), so no new store or schema.
- **BentoML is the reuse path for new CPU framework models** — embeddings and tabular reuse the vision
  BentoML pattern on CPU (keeps the gateway a thin proxy); whisper.cpp reuses the llama.cpp native-daemon pattern.
- **The GPU/FT stack stays frozen** — 009 adds serving deps (sentence-transformers, LightGBM) and a native
  engine (whisper.cpp); it does NOT move the torch cu128 / transformers training stack (Principle II / 007
  freeze stand).
- **CPU embeddings make RAG swap-free** — because embeddings serves on CPU off-lease, an embed→LLM RAG flow
  runs with no model swap (embed on CPU and the LLM on GPU never contend); the swap thrash is removed, not
  merely accepted.

## Non-Goals

- **A general multi-model-in-VRAM mode** — the single 008 GPU lease is unchanged; co-residency of two GPU
  modalities is out of scope.
- **AutoGluon as the default tabular engine** — LightGBM is the default; AutoGluon is documented as an
  optional, version-pinned, GBM-constrained upgrade path only (FR-081).
- **Non-CUDA / cloud ASR** — ASR is whisper.cpp native CUDA only; no cloud or CPU-only ASR backend.
- **A RAG pipeline / vector store** — 009 serves the embedding *modality*; wiring a retrieval store or a
  RAG chain is a separate increment.
- **GPU/FT stack movement** (torch/transformers/…) — frozen (Principle II / 007); a separate increment if
  ever.
- **New lifecycle stage** — all three modalities are serving/inference (an existing stage); none adds a
  stage requiring an amendment.
