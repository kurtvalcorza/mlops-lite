---
description: "Task list for Model Evaluation & Validation Gates (011)"
---

# Tasks: Model Evaluation & Validation Gates

**Input**: Design documents from `specs/011-evaluation-gates/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened, refreshed platform
(002/004/005/006/007). Adds an evaluation + gate to the promotion path — new code + light metric libs, no
new service or runtime.

**Tests**: Re-run the relevant 001–007 integration suite per phase on the target machine before the next.
Task IDs continue the shared space (T202+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **DRAFT — GRILLED (2026-06-28), build-ready.**
> Scope: the **keystone** of the MLOps-maturity layer — offline eval harness + gated promotion + offline
> champion-challenger. Uses **existing MLflow + gateway**; light metric libs only; **no online A/B** (one
> model in VRAM, Principle II); no constitution amendment (advances Principle VI, hardens Principle IV).
> Tasks T202–T221.
>
> **Decided (firm FRs):**
> 1. **Gate lives in the single `registry.promote` choke-point** — every alias move is gated, no back-door
>    (FR-105). Verdict (pass/warn/blocked + candidate vs incumbent metric + delta + threshold) returned by
>    the promote API + surfaced in Models/Runs UI.
> 2. **Champion-challenger is OFFLINE** — held-out scoring (sequential VRAM loads), the default and only
>    011 source; shadow-replay deferred → 013; never two models resident (FR-106, Principle II).
> 3. **Eval metrics log to existing MLflow** against the model version, with benchmark provenance (name +
>    version/hash) (FR-100/FR-101, Principle VI).
> 4. **Light metric libs only** — `jiwer` (WER), `sacrebleu`/`rouge` (text), sklearn metrics
>    (accuracy/top-k/AUC/recall@k); no heavy framework, no always-on worker (FR-102, Principle III).
> 5. **Gate is configurable** — hard-gate vs warn-on-regression + missing-metric policy + no-incumbent
>    pass; metric **direction** (higher/lower-is-better) + **like-for-like** comparison honoured
>    (FR-103/FR-104).
>
> **Grilled decisions (2026-06-28):**
> 1. **Per-modality metrics = sensible defaults + small bundled held-out fixtures, configurable** (mirrors
>    010's hyperparam defaults): **vision = top-1 accuracy** (higher-better); **ASR = WER** (lower-better,
>    `jiwer`); **embeddings = recall@k** (higher-better); **tabular = AUC** (higher-better); **LLM =
>    task-accuracy on a small QA held-out set** (higher-better) + **perplexity universal fallback**
>    (lower-better). LLM + vision **committed**; ASR/embeddings/tabular **guidance stubs** until served
>    (009). Resolves T202/T203 (FR-100/FR-101/FR-103).
> 2. **Default gate = hard-gate with a small regression tolerance + explicit override**, configurable down
>    to warn-only; missing-metric + no-incumbent policies stay. Safe-by-default closes the DIMER no-gate gap
>    without blocking on noise (FR-103/FR-104).
> 3. **Champion-challenger source = held-out set (default); shadow-replay DEFERRED** to a **013-dependent**
>    follow-on (needs 013's prediction + label logging). Recorded in Out of Scope (FR-106).

---

## Phase 0 — Pre-flight (gates everything)

- [ ] **T202** [US1] Confirm the light metric libs (`jiwer`, `sacrebleu` and/or `rouge-score`,
  `scikit-learn` metrics) resolve/install clean in the gateway image (dry build) and stay light — no heavy
  transitive pull (Principle III). Record the chosen set. (FR-102)
- [ ] **T203** [US1] Commit the **grilled per-modality primary metric + small bundled held-out benchmark
  fixture** (all configurable): **LLM = task-accuracy on a small QA held-out set** (higher-better) +
  **perplexity universal fallback** (lower-better) and **vision = top-1 accuracy** (higher-better) for the
  two *served* modalities; **guidance-stub** defaults for **ASR = WER** (lower-better), **embeddings =
  recall@k** (higher-better), **tabular = AUC** (higher-better) — implemented when those serving paths exist
  (009). Record each metric's **direction** and the benchmark's name + version/hash. (FR-100, FR-101,
  FR-103)

## Phase 1 — Offline eval harness (US1, P1) → SC-064

- [ ] **T204** [US1] Create `gateway/app/evaluation.py`: a per-modality **primary-metric** function set
  (LLM task-accuracy + perplexity fallback, vision top-1 accuracy; sklearn/jiwer/sacrebleu-backed), each
  tagged with its **direction**. Mirror `monitoring.py`'s dependency-light, MinIO/MLflow-reusing style.
  (FR-100, FR-102)
- [ ] **T205** [US1] In `evaluation.py`, implement `evaluate(model_version, benchmark)` → run the held-out
  benchmark through the **existing serving path** (one model in VRAM), compute the primary metric, and **log
  it to MLflow** against the model version with the **benchmark identifier** (name + version/hash) as
  metadata. (FR-100, FR-101, FR-107)
- [ ] **T206** [US1] Benchmark fixtures: add small held-out sets under `benchmarks/` (LLM + vision),
  content-addressed / hashed for provenance; wire the harness to load them via the existing dataset surface
  where practical. (FR-101)
- [ ] **T207** [P] [US1] (optional) `scripts/eval_model.py` — one-shot CLI entry to run the harness for
  batch eval / seeding the incumbent's metric. (FR-100)
- [ ] **T208** [P] [US1] `tests/test_eval_harness`: each supported modality computes its primary metric,
  logs it to MLflow against the version with benchmark provenance, and **re-running yields the same score**
  (reproducible). (SC-064)

## Phase 2 — Gated promotion (US2, P1) → SC-065 + SC-066 + SC-067

- [ ] **T209** [US2] In `evaluation.py`, implement `gate(name, candidate_version)` → fetch the candidate's
  + the incumbent's (`@serving`) logged eval metric, compare honouring **direction** + **like-for-like**
  (same modality + metric), compute the regression vs **tolerance**, and return a **GateVerdict**
  (`pass`/`warn`/`blocked` + candidate metric + incumbent metric + delta + tolerance + mode). Handle
  **no-incumbent** (pass) and **missing-metric** (policy) explicitly. (FR-103, FR-104)
- [ ] **T210** [US2] Wire the gate into `gateway/app/registry.py` `promote()`: consult `gate(...)` **before**
  `set_registered_model_alias`; in the **default hard-gate** mode a `blocked` verdict refuses the alias move
  (unless an explicit **override** flag is passed → moves, flagged), in **warn** mode it moves but the
  verdict is flagged. **Single choke-point — no ungated back-door.** Return the verdict from `promote()`.
  (FR-105)
- [ ] **T211** [US2] Make the gate mode + tolerance + **override flag** + missing-metric policy
  **configurable** (env / request param); **default = hard-gate with tolerance** per plan.md; the promote
  API response carries the verdict (candidate vs incumbent metric + delta + tolerance + mode). (FR-104,
  FR-105)
- [ ] **T212** [US2] **UI**: the Models/Runs surface shows each version's **eval metric** and, on a
  promotion, the **gate verdict** (pass / warn / blocked, with candidate vs incumbent metric + delta).
  (FR-105, SC-067)
- [ ] **T213** [P] [US2] `tests/test_promotion_gate`: regressing candidate → **refused** (alias unmoved) in
  the **default hard-gate**, **moved + flagged** with the explicit **override**, **flagged** (alias moved)
  in warn; non-regressing candidate promotes in all modes; **missing-metric** + **no-incumbent** hit their
  policies; verdict (candidate vs incumbent + delta + tolerance + mode) returned by the API. (SC-065,
  SC-066)
- [ ] **T214** [P] [US2] Confirm **no ungated path**: every alias move runs through the gate (the raw
  registry `promote` is the only mover and it consults the gate). (SC-066)

## Phase 3 — Champion-challenger via offline held-out eval (US3, P2) → SC-068

- [ ] **T215** [US3] In `evaluation.py`, implement `compare(champion, challenger)` (held-out, the **default
  and only 011 source**): score both the `@serving` champion and the challenger on the same held-out set,
  **loading sequentially** (one model in VRAM, releasing between), produce a per-metric comparison +
  declared **winner**. (FR-106)
- [ ] **T216** [US3] **DEFERRED → 013** — shadow-replay of logged inference requests (replay through the
  challenger, score against the same metric, compare to the champion's recorded performance) needs **013's
  prediction + label logging** to be meaningful; recorded as a 013-dependent follow-on, **out of scope for
  011**. (FR-106)
- [ ] **T217** [US3] **UI/CLI**: surface the champion-challenger comparison (per-metric table + winner);
  ensure the comparison's metric is the same number the gate (US2) uses. (FR-106)
- [ ] **T218** [P] [US3] `tests/test_champion_challenger`: held-out comparison yields a per-metric winner;
  assert the **VRAM mutex is never violated** (champion + challenger scored sequentially — at most one model
  resident). (SC-068)

## Phase 4 — Cross-cutting regression

- [ ] **T219** Full **001–007 keyed sweep** green with the eval/gate stack in place; serving, registry,
  datasets, training, drift, and tracing all unchanged. (SC-069)
- [ ] **T220** Confirm **footprint** unchanged in spirit: no always-on service added, the gateway image
  stays light (metric libs only), and the gate adds **no model load** to the promote call. (SC-069)
- [ ] **T221** Commit benchmark fixtures + `evaluation.py` + the `registry.py` gate hook + UI surfacing +
  `requirements.txt` metric-lib pins; record the chosen per-modality metrics + gate defaults in the spec.
  (SC-064–SC-069)

---

## Dependencies & Execution Order

- **T202–T203 (pre-flight) gate everything** — pick the metric libs + per-modality metric/benchmark before
  building the harness.
- **US1 (harness, T204–T208)** is the foundation — the gate (US2) and champion-challenger (US3) both
  consume its logged metric; do it first.
- **US2 (gate, T209–T214)** depends on US1's metric; it is the keystone (every promotion gated).
- **US3 (champion-challenger, T215–T218)** builds on US1's harness + US2's metric plumbing; P2, after the
  gate is in. Held-out only — shadow-replay (T216) is deferred to 013.
- **T219–T221 land last** (need every story in place).

### Constitution gates (re-check each phase)
- Principle II untouched: **no online A/B** — champion-challenger is offline, sequential loads; the gate
  loads no model (metric lookup only).
- Principle III honoured: light metric libs only, no heavy framework, no always-on worker.
- Principle IV hardened + Principle VI advanced: gated promotion + tracked eval metric per version.
- No new runtime → no amendment (Python/MLflow/Node all pre-existing; metric libs are pip deps).

## Implementation Strategy

1. **Pre-flight metric choices**, then **harness** → log a reproducible primary metric per version. **Stop
   and validate (SC-064).**
2. **Gate** → wire into the single `promote` choke-point, configurable mode, surface the verdict. **Stop and
   validate (SC-065/066/067).**
3. **Champion-challenger** → offline held-out + shadow-replay, sequential VRAM loads, surfaced. **Validate
   (SC-068).**
4. **Cross-cutting** → 001–007 no-regression sweep; footprint + VRAM-mutex intact (SC-069).

## Out of Scope (recorded)
- **Online A/B / live traffic split** — forbidden by Principle II; 011 is offline-only (not deferred,
  out of scope).
- **Shadow-replay champion-challenger** — **deferred to a 013-dependent follow-on** (needs 013's prediction
  + label logging to score replayed requests meaningfully); 011's champion-challenger is held-out only
  (T216).
- **Hyperparameter optimization** — 011 provides the target; HPO is increment 012.
- **Heavyweight eval framework / large benchmark suites** — 011 stays light (Principle III); a richer suite
  is a later increment.
- **Auto-promotion / drift→retrain→promote automation** — 011 gates a human-initiated promotion; wiring it
  into an automated loop is separate.
