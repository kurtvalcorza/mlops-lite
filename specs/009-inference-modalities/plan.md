# Implementation Plan: Inference Modalities & Task-Driven UI

**Branch**: `009-inference-modalities` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/009-inference-modalities/spec.md` (registry task/engine
metadata + a task-driven Infer panel + three new served modalities: embeddings, ASR, tabular)

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

**Grilled decisions (2026-06-28):** (a) **embeddings = CPU, off-lease, always-available BentoML service**
(corrected from GPU-lease tenant) — `serving_engine=bentoml`, `@bentoml.api(batchable=True)` for CPU
batching; CPU-embeddings removes the RAG embed→LLM swap thrash (embed on CPU + LLM on GPU never contend);
(b) **tabular = CPU, off-lease BentoML** LightGBM service; (c) **serving tier for both CPU modalities =
BentoML service** (not in-process pyfunc) for thin-gateway consistency with vision; (d) **the only new
GPU-lease tenant is ASR** (whisper.cpp). Embeddings *fine-tuning* still uses the GPU (train-on-GPU /
serve-on-CPU); only serving moves to CPU. No remaining open forks.

## Summary

An additive capability increment on top of 008's GPU lease. (US1, the **enabler**) tag every registered
model version with a `task` + `serving_engine`, route the gateway off that registry metadata, and render
the Infer tab's panels per task (dynamic). (US2) add an **embeddings** modality via a BentoML
`sentence_transformers` **CPU** service — off-lease, always available. (US3) add an **ASR** modality as a
**whisper.cpp native CUDA daemon under the supervisor**, mirroring the llama.cpp pattern and joining the
lease. (US4) add a **tabular** modality (LightGBM, single joblib artifact) via a BentoML **CPU** service —
off-lease, always available. Placement map: GPU lease (one tenant at a time) = LLM/vision/ASR/training;
CPU always-on, off-lease = embeddings/tabular. Phase-gated like 002/004/005/006/007, validated against the
full 001–008 suite each tier, never regressing the single-lease VRAM behavior and never moving the frozen
torch cu128 stack.

## Technical Context

**Language/Version**: Python (gateway 3.12, native serving/training venv), Node 20+ (UI/BFF). **No new
language or runtime** — whisper.cpp is a native CUDA binary under the existing supervisor (the same
hybrid-GPU native-host allowance as llama.cpp, v1.2.0).

**Primary Dependencies (new, additive)**:
- `sentence-transformers` — embedding models, packaged via MLflow's `sentence_transformers` flavor, served
  in a BentoML **CPU** service (`@bentoml.api(batchable=True)` for CPU batching); CPU-only, off-lease.
- **whisper.cpp** — native CUDA daemon built-from-source under the supervisor; the ASR engine. Mirrors
  the llama.cpp build/load-on-demand/idle-release/lease pattern exactly.
- **LightGBM** — light CPU dep, single joblib artifact, served via a BentoML CPU service. Default tabular.
- (doc-only) **AutoGluon** — optional upgrade path, GBM-constrained, `clone_for_deployment(model='best')`,
  version-pinned; NOT a runtime dep of the default path.

**Frozen (unchanged)**: torch `2.11.0+cu128`, torchvision, transformers/peft/accelerate/datasets — 009
adds serving/engine deps only; it does NOT touch the GPU/FT training stack (Principle II / 007 freeze).

**Routing model (US1)**: the gateway resolves a task's serving target from MLflow version tags —
`@serving` alias + `search_model_versions("tags.task='…'")` + the `serving_engine` tag — replacing any
hard-coded endpoint→model wiring. LoRA adapters inherit the base's `serving_engine`. This is the modern
alias/tag path already in `gateway/app/registry.py` (aliases + `create_model_version(tags=…)` /
`set_model_version_tag`), so **no new store/schema**.

**UI model (US1)**: the Infer tab queries the registry, discovers distinct `task` values, and renders one
panel per task via a renderer map keyed by task; an unknown task → read-only "no renderer" placeholder.
`embed` (embeddings) and `predict` (tabular) are always live; `stream`/`classify`/`transcribe` are
lease-governed (008).

**Storage**: no new backing store. Models/artifacts live where they already do (MinIO `models` bucket;
registry pointers in MLflow). Tabular = one joblib artifact; embeddings via the `sentence_transformers`
flavor; whisper.cpp model weights load on demand like llama.cpp.

**Target Platform**: Win11 + WSL2 + Rancher Desktop. Gateway/MLflow/infra in Docker; LLM serving,
training, BentoML services, the new whisper.cpp daemon, and the UI run **native in WSL** (GPU passthrough
+ disk-frugality), bound to localhost. whisper.cpp joins the native supervisor alongside llama.cpp/bento/ui.

**Performance Goals**: none new; the single-lease swap cost is the existing 008 behavior. `@bentoml.api(
batchable=True)` is for embedding throughput. Must not regress inference latency or the lease hold time.

**Constraints**: single GPU lease (008) — at most one GPU-resident model; tabular CPU-only/off-lease;
additive + swappable deps; frozen torch stack; loopback/auth/BFF posture unchanged; behavior-preserving
for the existing LLM/vision flows.

## Constitution Check

*GATE: Must pass before design. Re-check after. Gated against constitution **v1.4.0** (008 amended
Principle II to the single-GPU-lease model: one tenant — LLM/vision/ASR OR training — at any
instant; CPU-only models exempt).*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | All new modalities run on the host (BentoML native in WSL, whisper.cpp native CUDA); nothing leaves the box | ✅ |
| II. Single-GPU Lease (v1.4.0, NON-NEGOTIABLE) | **ASR** joins the single lease as a tenant (swap/disable per 008); **embeddings + tabular are CPU-only, exempt**; no new VRAM assumption; torch cu128 frozen | ✅ honored |
| III. Lightweight Footprint | whisper.cpp = native, no resident cost until used + idle-release; LightGBM = light CPU dep; embeddings reuse the BentoML runtime; no idle-RAM blowout | ✅ |
| IV. Full Lifecycle Coverage | No stage added/dropped — all three are serving/inference (existing stage) | ✅ N/A |
| V. OSS & Swappable | sentence-transformers / whisper.cpp / LightGBM are mainstream OSS behind the existing BentoML/supervisor interfaces; AutoGluon documented as a swap | ✅ |
| VI. Reproducibility & Observability | Every modality registered in MLflow with `task`/`serving_engine` tags; each exposes health/readiness; routing is recorded metadata | ✅ strengthened |
| VII. Phase-Gated Delivery | US1 enabler then three independently-runnable modality stories (US2–US4), each re-validated on the target machine | ✅ |
| Workflow: "no new runtime without amendment" | None — whisper.cpp is a native GPU service already allowed (v1.2.0); BentoML/Python/Node all pre-existing | ✅ no amendment |

**No amendment required.** 009 adds one tenant (ASR) to the existing 008 lease and two CPU-exempt
modalities (embeddings, tabular); it introduces no new runtime (whisper.cpp is permitted under the v1.2.0
hybrid-GPU amendment; the rest reuse BentoML). Clean gate-check vs v1.4.0, mirroring 005/006/007.

## Project Structure

### Source Code (delta over 008)

```text
mlops-lite/
├── gateway/app/
│   ├── registry.py               # MODIFIED: task/serving_engine tag helpers; search_model_versions("tags.task='…'"); resolve-by-task
│   ├── routers/
│   │   ├── embed.py              # NEW: POST /embed → BentoML CPU embeddings service (thin proxy, off-lease, mirrors vision.py)
│   │   ├── transcribe.py         # NEW: POST /transcribe → whisper.cpp daemon (audio → text)
│   │   ├── tabular.py            # NEW: POST /predict → BentoML CPU tabular service (off-lease)
│   │   └── infer.py / serving.py # MODIFIED: resolve serving target from registry task metadata (not hard-coded)
│   └── main.py                   # MODIFIED: mount embed/transcribe/tabular routers
├── serving/
│   ├── bento/
│   │   ├── embed_service.py      # NEW: BentoML CPU sentence-transformers service, embed(texts)->vectors, batchable=True (off-lease)
│   │   ├── tabular_service.py    # NEW: BentoML CPU LightGBM service, predict(rows: list[dict])
│   │   └── requirements.txt      # MODIFIED: + sentence-transformers, + lightgbm (CPU)
│   └── whispercpp/               # NEW: build-from-source CUDA + run.sh (mirrors serving/llamacpp pattern)
│       ├── build.sh              # NEW: cmake CUDA build of whisper.cpp
│       └── run.sh                # NEW: load-on-demand server launch (supervisor target)
├── supervisor/ (or scripts/)     # MODIFIED: register the whisper.cpp daemon as a supervised GPU-lease tenant
├── ui/app/                       # MODIFIED: Infer tab → dynamic per-task panel renderer map
│   └── components/infer/         # NEW: renderers — embed/transcribe/predict panels + "no renderer" fallback
├── scripts/
│   ├── seed_vision_model.py      # MODIFIED: register vision with task/serving_engine tags
│   ├── seed_embedding_model.py   # NEW: register the sentence-transformer with task=embedding tags
│   ├── seed_tabular_model.py     # NEW: train/seed a LightGBM joblib artifact + register task=tabular
│   └── retag_serving_llm.py      # NEW (or fold into reseed): add task=text-generation/serving_engine=llama.cpp
└── tests/                        # NEW: test_embed, test_transcribe, test_tabular, test_registry_tasks, test_infer_panels
```

**Structure Decision**: keep each modality in its own service/router/seed/test files so a regression
bisects to a modality. US1 is the only cross-cutting change (registry routing + UI panel map); US2–US4
are additive leaves that depend on it.

## Phasing (maps to constitution VII)

- **Phase 0 — Pre-flight**: confirm `sentence-transformers` + `lightgbm` install clean in the BentoML
  native venv; confirm whisper.cpp builds with CUDA on the host (gate zero: `nvidia-smi` in the GPU env);
  confirm `search_model_versions("tags.task='…'")` works against the 3.x server (007).
- **Phase 1 — Enabler: registry metadata + routing + dynamic panel (US1, P1)**: add `task`/`serving_engine`
  tag helpers to `registry.py`; route the gateway off task metadata (resolve-by-task, LoRA inherits base
  engine); retag/reseed the serving LLM + vision; build the Infer tab's per-task panel renderer map +
  "no renderer" fallback. Exit: SC-049 + SC-050 + SC-051.
- **Phase 2 — Embeddings (US2, P2)**: BentoML **CPU** `sentence_transformers` service (`embed`, batchable);
  seed + register `task=embedding`; gateway `POST /embed` proxy; embed panel (always live); validate
  off-lease/always-available (`/embed` succeeds while a GPU tenant holds the lease) + batching. Exit: SC-052.
- **Phase 3 — ASR via whisper.cpp (US3, P2)**: build whisper.cpp CUDA from source; register it under the
  supervisor as a GPU-lease tenant (load-on-demand, idle-release); seed + register `task=asr`; gateway
  `POST /transcribe` proxy; transcribe panel; validate lease swap + VRAM release. Exit: SC-053.
- **Phase 4 — Tabular (US4, P3)**: BentoML CPU LightGBM service (`predict`); seed a joblib artifact +
  register `task=tabular`; gateway `POST /predict` proxy; predict panel (always live); document AutoGluon
  as the optional upgrade path. Validate off-lease/always-available. Exit: SC-054 + SC-055.
- **Phase 5 — Cross-cutting regression**: full 001–008 no-regression sweep; confirm the placement map
  holds, the single-lease swap is intact, SSE framing + existing tabs unchanged, torch cu128 frozen.
  Exit: SC-056.

Cross-cutting: each phase re-runs the relevant 001–008 tests on the target machine; the single GPU lease
and frozen GPU stack are re-checked at every gate.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Route off registry `task`/`serving_engine` tags (US1) | Makes modalities pluggable — adding one = register + small renderer, not gateway re-plumbing; uses the existing alias/tag path | A hard-coded endpoint→model map works for 2 modalities but doesn't scale to 5 and couples the gateway to each model |
| Embeddings via a BentoML **CPU** service (vs in-gateway pyfunc) | Keeps the gateway a thin proxy; consistent with vision/tabular; loads the embedding framework out of the gateway process; CPU/off-lease so it never contends for the GPU | **GRILLED — RESOLVED (2026-06-28): BentoML CPU service** for both CPU modalities (embeddings + tabular). In-process MLflow pyfunc is lighter but pulls the framework into the gateway and breaks thin-gateway consistency with vision; rejected |
| whisper.cpp as a native CUDA supervisor daemon | Mirrors llama.cpp exactly (build-from-source, load-on-demand, idle-release, lease); native GPU path already allowed (v1.2.0) | A Python ASR lib in the gateway/Bento would pull a heavy GPU runtime into a non-native path and break the one-pattern-for-GPU-engines symmetry |
| LightGBM default (single joblib) + AutoGluon as doc-only upgrade | Lightest tabular path (one CPU artifact, off-lease, always-on); AutoGluon's GBM-constrained clone is a drop-in when needed | Making AutoGluon the default pulls a heavy multi-model framework + version-pinning burden into the default path — disproportionate for Principle III |
| Tabular CPU/off-lease/always-available | A `predict` must work even while a GPU tenant holds the lease; CPU keeps it exempt from Principle II | Putting tabular on the GPU lease would needlessly serialize a CPU workload behind GPU swaps |
| Single GPU lease unchanged (ASR is the only new tenant) | Honors 008 v1.4.0 — one GPU-resident model at a time; embeddings on CPU means RAG embed→LLM runs with no swap | A co-residency exception would breach Principle II (the project's core constraint) for a single-operator machine |
