# Feature Specification: Score-at-Registration — every version born with its eval metric (closing SC-068)

**Feature Branch**: `015-on-demand-version-loading`

**Created**: 2026-06-30

**Status**: **BUILT (offline, 2026-06-30) — on-hardware SCs (SC-087/088/089/090/091/092) pending the
RTX 5070 Ti box.** Code + offline unit tests landed and green (no regression). The grill pivoted the
approach: instead of teaching every serving daemon to load any requested version on demand (heavy,
multi-daemon), 015 **scores each model version in-process at registration** so every version is *born*
with its logged eval metric, and the gate / compare / quality / HPO read those logged metrics. This is
smaller, and it makes the trainer→daemon-URL gap (finding #4) **moot** — trainer-side scoring never calls
a serving daemon.

> **Grilled decisions (2026-06-30):**
> 1. **Trainer-side scoring = in-process.** The HPO objective and any trainer-side eval score the
>    just-trained model **in the trainer subprocess** via the eval harness's injectable `predict_fn` —
>    not by asking a serving daemon to load the version. Sidesteps SC-068 and finding #4 for the hot path.
> 2. **Score-at-registration for ALL fine-tunes** (not just HPO). Every fine-tune (LLM/vision/embeddings/
>    ASR) scores its model on its modality's held-out benchmark and **logs the eval metric at registration**.
>    ⇒ every registered version carries its metric; gate/compare/quality read logged metrics; the
>    "unevaluated incumbent" case (fixed in PR #20) becomes rare by construction.
> 3. **Gateway standalone `/evaluate` + `/compare` = GUARD, not load.** No per-version daemon-loading
>    machinery is built. `compare`/gate/quality read logged metrics. Gateway `/evaluate` scores the
>    `@serving` model; asked for a *different* version that has no logged metric, it returns a **clear
>    error** ("serve/promote it, or it was scored at registration") — never a silent score of the wrong
>    (resident) model (the SC-068 mislabel this closes).
> 4. **Modality scope = all four trainable** (LLM, vision, embeddings, ASR). 015 therefore **commits the
>    embeddings (recall@k) and ASR (WER) held-out benchmark fixtures and finalizes those metrics** (today
>    they are 011 "guidance stubs"). Tabular has no fine-tune flow (doc-only) → out of scope.
> 5. **LLM scores the SERVED artifact**: convert adapter→GGUF (already done at registration), then load
>    base+adapter in a **transient llama.cpp** and score — measures the exact quantized GGUF that gets
>    served, not the unquantized HF weights.
> 6. **ASR mirrors the LLM**: score the served **ggml via a transient whisper.cpp**. **Vision + embeddings
>    score in-memory** (the in-memory torch / sentence-transformers model *is* what's served — no
>    quantization gap).
> 7. **Scoring runs within the fine-tune's existing lease hold** (train → free training model → load
>    served artifact → score → release, **once**). One model in VRAM at any instant (Principle II, v1.4.0,
>    preserved — sequential within one hold). **No constitution amendment.** Extends the fine-tune's
>    lease-hold by the scoring time. **Batch (014) is OUT of SC-068 scope** — batch-inferring a dataset
>    with the `@serving` model is correct production behavior, not a mislabel.

**Input**: On-hardware validation of 011–014 (2026-06-30) confirmed SC-068 is the keystone blocker. The
offline scoring paths scored **whichever version the serving daemon currently holds**, not the requested
registered version (identifiers recorded for provenance only). Live: `compare()` of a non-resident
challenger was degenerate; `evaluate()` could log the incumbent's score against a challenger; **012 HPO's
per-trial objective was meaningless** (every trial scored the same resident model) and additionally failed
with `[Errno -2] Name or service not known` because the native trainer can't reach the serving daemons'
Docker hostnames (finding #4). The grilled in-process design closes both.

> **Scope note**: 015 **hardens Principle VI (Reproducibility & Observability)** — "score the thing you
> say you scored." No new lifecycle stage, no new service, no new runtime. It extends the **existing**
> fine-tune flows (010) + HPO (012) with an in-process scoring pass, finalizes two 011 benchmark fixtures,
> and adds a gateway guard. Requirement IDs continue the shared space (FR-137+, SC-087+, tasks T276+).
> **No constitution amendment** (grilled decision 7). See plan.md → Constitution Check.

> **Hard boundary (NON-NEGOTIABLE)**: **Principle II — one GPU tenant under the single race-free lease
> (008, v1.4.0)** is preserved: scoring is sequential within the fine-tune's existing lease hold, never a
> second resident model. The **frozen Blackwell sm_120 GPU stack** (torch/torchvision cu128 +
> transformers/peft/accelerate/datasets) is **NOT** touched, and **no heavy new dependency** is added
> (Principle III) — scoring reuses the already-built llama.cpp / whisper.cpp / torch and 011's pure-Python
> metrics. Reuse the existing MLflow registry, trainer, gateway, and lease.

> **Builds on**: 008 GPU lease (v1.4.0) · 009 task/`serving_engine` routing · 010 multimodal fine-tune ·
> 011 eval harness + gate (incl. PR #20 unevaluated-incumbent fix + PR #21 eval-against-served fixes) ·
> 012 HPO · 013 quality.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Every fine-tuned version is born with its eval metric (Priority: P1)

A fine-tune (any modality) trains the model and, **before releasing the GPU lease**, scores it on its
modality's held-out benchmark and logs the eval metric against the new registry version. The version is
registered already carrying its `eval_*` metric tags — no separate evaluate step, no "unevaluated" window.

**Why this priority**: This is the core. It makes every downstream consumer (gate, compare, quality, HPO
objective) correct by construction, and it is the in-process mechanism the others reuse. It leads.

**Independent Test**: Fine-tune two behaviorally-distinct versions of one model; confirm each registers
with a **logged eval metric tied to that version** and the two metrics differ (each scored its own model,
not a shared resident one). Confirm `nvidia-smi` never shows two models resident during the train→score
sequence.

**Acceptance Scenarios**:

1. **Given** a fine-tune of any in-scope modality, **When** it completes, **Then** the registered version
   carries a logged eval metric (its modality's primary metric + direction + benchmark provenance) scored
   on that version's own weights/artifact.
2. **Given** the LLM (or ASR) modality, **When** scored, **Then** scoring loads the **served artifact**
   (GGUF via llama.cpp / ggml via whisper.cpp), after the training model is freed — one model in VRAM.
3. **Given** a fine-tune holding the lease, **When** it transitions train→score, **Then** it does not
   release the lease between, and never holds two models in VRAM at once.

---

### User Story 2 — Meaningful HPO objective and champion-challenger (Priority: P1)

Each HPO trial's objective is **its own trained version's** logged metric (from US1's in-process score),
so the study optimizes toward the genuinely best candidate and registers it. `compare(champion,
challenger)` reads **both versions' logged metrics** and declares a real winner — no model reload, no
degenerate champion ≈ challenger.

**Why this priority**: This is the payoff that fixes the two features the live run proved degenerate (012
HPO, 011 compare). It depends entirely on US1.

**Independent Test**: Run a 2-trial HPO study whose trials produce measurably different models; confirm
the trials log **distinct** objective values, the registered "best" is the genuinely better one, and the
trainer-side eval completes with **no hostname-resolution error** (finding #4 moot).

**Acceptance Scenarios**:

1. **Given** an N-trial HPO study, **When** it runs, **Then** each trial's objective is its own version's
   in-process score and the best-by-objective version is registered; no `host.docker.internal` call occurs.
2. **Given** a `@serving` champion and a distinct challenger both carrying logged metrics, **When**
   `compare()` runs, **Then** it returns a verdict reflecting their real metric gap by reading logged
   metrics (no model reload).

---

### User Story 3 — Gateway standalone eval/compare is correct or refuses (Priority: P2)

The gateway `/models/{name}/evaluate` and `/compare` never silently score the wrong (resident) model.
`compare` reads logged metrics. `/evaluate` scores the `@serving` model; if asked for a **different**
version that has **no** logged metric, it returns a **clear error** directing the operator to serve/promote
it (or noting it was scored at registration) — closing the SC-068 mislabel without per-version daemon loading.

**Why this priority**: It hardens the operator-facing surface against the exact mislabel the live run hit,
with a small guard rather than heavy machinery.

**Independent Test**: Call `/evaluate` for a non-`@serving` version with no logged metric → assert a clear
error (not a score). Call it for a version that has a logged metric → returns/uses that metric. Call
`/compare` on two scored versions → reads logged metrics, no reload.

**Acceptance Scenarios**:

1. **Given** a version that is not `@serving` and has no logged metric, **When** `/evaluate` is called for
   it, **Then** it returns a clear error (never a silent resident-model score).
2. **Given** two versions that both carry logged metrics, **When** `/compare` runs, **Then** it judges from
   the logged metrics with no model reload.

---

### Edge Cases

- **Train model not freed before scoring** (LLM/ASR): scoring must free the HF training model + optimizer
  (empty_cache) before loading the served artifact, or it would breach one-model-in-VRAM. Hard invariant.
- **Scoring failure**: a fine-tune whose training succeeds but scoring fails MUST surface the failure
  clearly (the version may register without a metric → the gate's missing-metric policy applies, PR #20).
  Decide: fail the whole run, or register-without-metric-and-warn? *(plan-level — recommend warn+register.)*
- **Transient llama.cpp/whisper.cpp scoring**: must release VRAM promptly (load → score → free) and not
  leave a scoring server resident or holding the lease past the fine-tune.
- **Benchmark availability in the trainer**: the held-out fixtures must be reachable from the **native
  trainer** (repo path — no Docker mount needed, unlike the gateway's bind-mount in PR #21).
- **Embeddings/ASR new fixtures**: 015 ships their benchmark JSONL + finalizes recall@k / WER scoring; the
  fixtures must be tiny + content-hashed for provenance like the LLM/vision ones.
- **Gateway `/evaluate` on the `@serving` version**: still scores it via the serving daemon (the resident
  model IS the requested one) — that path is correct and stays.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-137**: Every in-scope fine-tune (LLM, vision, embeddings, ASR) MUST score its model on the
  modality's held-out benchmark and **log the eval metric against the new registry version at
  registration**, within the fine-tune's existing GPU-lease hold.
- **FR-138**: Trainer-side scoring MUST run **in-process** (the just-trained model / its served artifact),
  NOT by calling a serving daemon — so no Docker-only hostname is used (finding #4 moot).
- **FR-139**: LLM scoring MUST use the **served GGUF** via a transient llama.cpp; ASR scoring MUST use the
  **served ggml** via a transient whisper.cpp; vision and embeddings MUST score the **in-memory** trained
  model. The training model MUST be freed before a served-artifact model is loaded (one model in VRAM).
- **FR-140**: Scoring MUST occur **within the fine-tune's existing lease hold** (no release between train
  and score) and hold **at most one model in VRAM** at any instant (Principle II).
- **FR-141**: The HPO objective MUST be **the trial's own registered version's logged metric** (from
  FR-137), so the study optimizes toward the genuinely best candidate.
- **FR-142**: `compare()` and the promotion gate MUST judge from **logged metrics** of the named versions
  (no model reload). Quality (013) MUST likewise read logged/served-prediction data, unchanged.
- **FR-143**: The gateway `/models/{name}/evaluate` MUST NOT silently score the resident model for a
  different requested version: if the requested version is not `@serving` and has no logged metric, it
  MUST return a **clear error**.
- **FR-144**: 015 MUST ship the **embeddings (recall@k) and ASR (WER) held-out benchmark fixtures** and
  finalize those metrics (content-hashed, tiny), so all four modalities can score at registration.
- **FR-145**: The change MUST add **no new always-on service, no new runtime, and no heavy dependency**,
  MUST NOT move the frozen GPU stack, and MUST keep batch (014) scoring the `@serving` model (out of scope).

### Key Entities

- **Registration-time EvalResult**: a version's primary metric + direction + benchmark name/digest, logged
  at fine-tune registration (reuses 011's `_log_eval` tags + run metric).
- **In-process predict_fn (per modality)**: scores benchmark rows against the trained model / served
  artifact in the trainer — vision/embeddings in-memory torch; LLM via transient llama.cpp; ASR via
  transient whisper.cpp.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-087**: Two behaviorally-distinct fine-tuned versions of one model register with **distinct logged
  metrics**, each scored on its own model (not a shared resident one).
- **SC-088**: During any train→score sequence, **at most one model is resident in VRAM** at any instant
  (`nvidia-smi` confirms) — Principle II preserved.
- **SC-089**: An HPO study's trials log **distinct** objective values, the registered "best" is the
  genuinely best, and the run completes with **no hostname-resolution error** (finding #4 closed).
- **SC-090**: `compare()` of two distinct, scored versions returns a verdict reflecting their real metric
  gap, reading logged metrics with **no model reload**.
- **SC-091**: Gateway `/evaluate` for a non-`@serving` version with no logged metric returns a **clear
  error**, never a silent resident-model score.
- **SC-092**: All four trainable modalities (LLM/vision/embeddings/ASR) score at registration on shipped,
  content-hashed benchmark fixtures.
- **SC-093**: The frozen GPU stack + dependency footprint are unchanged; 001–014 suites still green
  (no-regression).

## Assumptions

- The single GPU lease (008) remains the sole VRAM-admission point; scoring runs inside the fine-tune's
  existing hold rather than introducing a second mechanism.
- The native trainer can run the served-artifact scorers it already has built (llama.cpp, whisper.cpp) and
  import 011's pure-Python metrics + the eval harness's `predict_fn` seam.
- The held-out fixtures are reachable from the native trainer via the repo path (no Docker mount needed).
- Shadow-replay of logged inference remains **out of scope** (deferred, 013-dependent).

## Non-Goals

- **No per-version on-demand loading in the serving daemons** (the grill replaced this with in-process
  score-at-registration).
- **No online A/B / live traffic split** (Principle II).
- **No shadow-replay** of logged inference (deferred).
- **No change to batch (014)** — scoring the `@serving` model is correct production behavior.
- **No change to the gate/metric math** (011) or the fine-tune training mechanics (010/012).
- **No constitution amendment.**
