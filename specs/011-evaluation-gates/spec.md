# Feature Specification: Model Evaluation & Validation Gates

**Feature Branch**: `011-evaluation-gates`

**Created**: 2026-06-28

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

> **Grilled decisions (2026-06-28)** — resolves this spec's three open-for-the-grill items:
> 1. **Per-modality metrics = sensible defaults + small bundled held-out fixtures, configurable** (mirrors
>    010's hyperparam approach — defaults, not over-specification). Default primary metric per modality, each
>    with its **direction** + a small bundled held-out fixture, all operator-configurable: **vision = top-1
>    accuracy** (higher-better); **ASR = WER** (lower-better, `jiwer`); **embeddings = recall@k**
>    (higher-better); **tabular = AUC** (higher-better); **LLM = task-accuracy on a small QA held-out set**
>    (higher-better) with **perplexity as a universal fallback** (lower-better). For the two currently-served
>    modalities (**LLM, vision**) the metric + fixture are **committed**; ASR / embeddings / tabular are
>    **guidance stubs**, implemented when their serving paths exist (009).
> 2. **Default gate mode = hard-gate with a small regression tolerance + explicit override**, configurable
>    down to warn-only. Default: **BLOCK** a promotion whose candidate metric regresses beyond a small
>    tolerance vs the incumbent `@serving` (honouring direction + like-for-like). An explicit operator
>    **override** flag bypasses a block; a config switches the whole gate to **warn-on-regression**.
>    Safe-by-default — closes the DIMER no-gate gap without blocking on noise. (FR-103/FR-104 configurability
>    stays; this fixes the *default*.)
> 3. **Champion-challenger source = held-out set (default); shadow-replay DEFERRED.** Default comparison is
>    the offline held-out benchmark (sequential VRAM loads, reproducible, no traffic/label dependency).
>    **Shadow-replay** of logged inference requests is **deferred** — it needs **013's** prediction + label
>    logging to be meaningful; recorded as a 013-dependent follow-on (Non-Goals / Out-of-Scope).
>
> Firm decisions intact: gate in the single `registry.promote` choke-point (FR-105); offline-only, no online
> A/B (Principle II); light metric libs (FR-102); advances Principle VI / hardens Principle IV; **no
> amendment**.

**Input**: Roadmap keystone for the MLOps-maturity layer. The platform can register many model versions and
promote one to `@serving` (since 004, via the modern alias path in `gateway/app/registry.py`), but
**promotion is ungated** — `promote(name, version)` moves the alias on command, with no check that the new
version is actually *better* (or even non-broken). This is the exact gap that sank DIMER by omission: it
served whatever last registered. 011 adds the **connective tissue** — an offline evaluation harness that
scores each model version on a held-out benchmark and logs the metric to MLflow, a **gate** that compares a
candidate against the serving incumbent *before* the alias moves, and an **offline champion-challenger**
comparison (the platform cannot do online A/B — one model in VRAM at a time). 011 also gives the future HPO
increment (012) a concrete target to optimize.

> **Scope note**: 011 adds an **evaluation + validation stage** to the lifecycle's promotion path. It does
> **not** add a new always-on service or runtime — the harness is gateway-side code (or a one-shot script)
> plus a few **light** metric libraries (Principle III). It uses the **existing** MLflow (tracking +
> alias-registry) and gateway. Requirement IDs continue the shared space (FR-100+, SC-064+, tasks T202+).
> **No constitution amendment** — evaluation logging *advances* Principle VI (Reproducibility &
> Observability: "if it isn't tracked, it didn't happen") and the gate hardens Principle IV (Full Lifecycle
> Coverage) without dropping or adding a top-level stage. See plan.md → Constitution Check.

> **Hard boundary (one model in VRAM, NON-NEGOTIABLE)**: Principle II forbids two models resident at once,
> so there is **NO online A/B / live traffic-split**. Champion-challenger is **offline** — both are scored
> on the same held-out set (loaded one at a time). Shadow-replay of logged inference requests is **deferred
> to a 013-dependent follow-on** (needs prediction + label logging to be meaningful). The gate and the
> comparison are batch/offline operations, never concurrent serving.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Offline evaluation harness per modality (Priority: P1)

An operator points the harness at a registered model version and a held-out benchmark for that modality; it
computes a **primary metric** for the task and **logs that metric to MLflow** against the model version, so
every version carries a comparable, reproducible score. Each modality has a **default primary metric +
direction + a small bundled held-out fixture** (all configurable, mirroring 010's hyperparam defaults):
**LLM = task-accuracy on a small QA held-out set** (higher-better), with **perplexity as a universal
fallback** (lower-better); **vision = top-1 accuracy** (higher-better); **ASR = WER** (lower-better,
`jiwer`); **embeddings = recall@k** (higher-better); **tabular = AUC** (higher-better). The two
currently-served modalities (**LLM, vision**) have committed metric + fixture; ASR / embeddings / tabular are
**guidance stubs**, implemented when their serving paths exist (009).

**Why this priority**: A gate needs a number to gate on. Without a tracked eval metric per version there is
nothing to compare, and HPO (012) has no target. This is the foundation the other two stories stand on, and
it directly advances Principle VI (every model version tracked, reproducible from its config).

**Independent Test**: For each supported modality, run the harness against a known model version + a small
held-out benchmark; the primary metric is computed, written to MLflow against that version (visible in the
Runs/Models UI), and is reproducible (re-running on the same version + benchmark yields the same score).

**Acceptance Scenarios**:

1. **Given** a registered serving-LLM version and a small held-out prompt/answer set, **When** the harness
   runs, **Then** a primary task metric (e.g. task accuracy / perplexity) is computed and logged to MLflow
   against that model version.
2. **Given** a registered vision-classifier version and a labelled held-out image set, **When** the harness
   runs, **Then** top-1 accuracy is computed and logged against that version.
3. **Given** a model version that has already been evaluated, **When** the harness re-runs on the same
   benchmark, **Then** the logged metric matches (deterministic / reproducible), and the eval is associated
   with a recorded benchmark identifier (name + version/hash).

---

### User Story 2 — Gated promotion (Priority: P1)

Before `promote` moves the `@serving` alias to a candidate version, the platform **compares the candidate's
eval metric against the current incumbent's**. **By default it hard-gates**: a candidate that regresses
beyond a small tolerance is **refused**, unless the operator passes an explicit **override** flag. The gate
is configurable down to **warn-on-regression** (allow but flag). The verdict — carrying candidate metric,
incumbent metric, delta, tolerance, and mode — is surfaced in the Models/Runs UI alongside the eval metric.

**Why this priority**: This is the keystone — it turns promotion from "move the pointer" into a
**responsible** operation, closing the DIMER-by-omission gap. It is P1 because the harness (US1) is inert
without something consuming its metric, and the gate is the whole point of 011.

**Independent Test**: With a serving incumbent that has a logged metric, attempt to promote a candidate
whose metric is *worse* by more than the configured tolerance: in the **default hard-gate** mode the
promotion is refused with a clear verdict (unless an explicit **override** flag is passed, which proceeds
flagged); in warn mode it proceeds but is flagged. Promoting a candidate that is *better* (or within
tolerance) succeeds cleanly in both modes. The Models UI shows each version's metric + the gate verdict.

**Acceptance Scenarios**:

1. **Given** a serving incumbent with eval metric `M0` and a candidate with metric `M1` worse than `M0` by
   more than the tolerance, **When** promotion is attempted in the **default hard-gate** mode (no override),
   **Then** the alias does **not** move and the response states the regression (candidate vs incumbent,
   delta, tolerance, mode).
2. **Given** the same regression with the explicit **override** flag (or with the gate configured to
   **warn-on-regression**), **When** promotion is attempted, **Then** the alias **does** move but the
   response/UI is **flagged** as a promoted-with-regression event.
3. **Given** a candidate at least as good as the incumbent (within tolerance), **When** promotion is
   attempted in either mode, **Then** it succeeds and the verdict is "pass".
4. **Given** a candidate or incumbent with **no logged eval metric**, **When** promotion is attempted,
   **Then** the gate applies its missing-metric policy (configurable: block vs warn) rather than silently
   passing.
5. **Given** any gate outcome, **When** the Models/Runs UI is viewed, **Then** the candidate's metric, the
   incumbent's metric, and the gate verdict (pass / warn / blocked) are shown.

---

### User Story 3 — Champion-challenger via offline held-out eval (Priority: P2)

Because one-model-in-VRAM forbids serving two models concurrently, the operator compares a **champion**
(current `@serving`) against a **challenger** (a candidate version) **offline** by scoring both on the same
**held-out benchmark** (the default and only 011 source — sequential VRAM loads, reproducible, no traffic or
label dependency). The comparison (per-metric, with a winner) is surfaced. (**Shadow-replay** of logged
inference requests is **deferred** — it needs 013's prediction + label logging; see Non-Goals.)

**Why this priority**: This is the natural extension of the gate — a deliberate, side-by-side champion vs
challenger read before promotion — but it builds on US1's harness and US2's metric plumbing, so it is P2.
It honours Principle II by being strictly offline (no concurrent serving, no live traffic split).

**Independent Test**: Pick a champion (`@serving`) and a challenger version; run the offline comparison on a
held-out set; the output is a per-metric table with each model's score and a declared winner, surfaced in
the UI/CLI, with **never** two models resident in VRAM at once.

**Acceptance Scenarios**:

1. **Given** a champion `@serving` version and a challenger version, **When** the offline comparison runs on
   a shared held-out set, **Then** a per-metric comparison with a winner is produced and surfaced, and the
   VRAM mutex is respected throughout (models scored one at a time).
2. **Given** the comparison result, **When** the operator promotes the challenger, **Then** the gate (US2)
   uses the comparison's metric — the champion-challenger read and the promotion gate agree on the number.

---

### Edge Cases

- **No incumbent yet**: promoting the *first* version (no `@serving` exists) has no incumbent to compare —
  the gate passes by definition (records "no incumbent"), it does not error (FR-104).
- **Missing eval metric**: a candidate (or incumbent) without a logged metric hits the gate's
  missing-metric policy (FR-104) — never a silent pass; the default and the override are explicit.
- **Higher-is-better vs lower-is-better**: WER and perplexity are *lower-is-better*; accuracy/AUC/recall@k
  are *higher-is-better*. The gate MUST know each metric's direction so a "regression" is computed
  correctly (FR-103).
- **Cross-modality mismatch**: comparing a vision metric against an LLM metric is meaningless — the gate
  compares **like-for-like** (same modality + same primary metric) or refuses to judge (FR-103).
- **VRAM during champion-challenger**: the comparison MUST load champion and challenger **sequentially**,
  releasing VRAM between them — never two at once (Principle II, FR-106).
- **Benchmark provenance**: an eval score is only meaningful with its benchmark — the harness records the
  benchmark identifier (name + version/hash) with the metric so a score is reproducible (FR-101, Principle
  VI).
- **Light footprint**: metric libraries stay small (`jiwer` for WER, `sacrebleu`/`rouge` for text, sklearn
  metrics for vision/tabular) — no heavy eval framework lands in the gateway image (Principle III, FR-107).
- **Gate fails open vs closed**: the gate is a *validation* step, not a serving path — if the eval/registry
  lookup itself errors, the gate's failure mode is explicit (default block, since an unverified promotion is
  the very risk 011 removes) (FR-104).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-100**: The platform MUST provide an **offline evaluation harness** that, given a registered model
  version and a held-out benchmark for its modality, computes a **primary metric** for the task and **logs
  it to MLflow** against that model version (metric value + benchmark identifier as run/version metadata).
  Each modality has a **default primary metric + direction + a small bundled held-out fixture**, all
  **configurable** (mirroring 010's hyperparam defaults): **LLM = task-accuracy on a small QA held-out set**
  (higher-better) with **perplexity as a universal fallback** (lower-better); **vision = top-1 accuracy**
  (higher-better); **ASR = WER** (lower-better); **embeddings = recall@k** (higher-better); **tabular = AUC**
  (higher-better). For the two currently-served modalities (**LLM, vision**) the metric + fixture are
  **committed**; ASR / embeddings / tabular are **guidance stubs**, implemented when their serving paths
  exist (009).
- **FR-101**: Each logged eval MUST record its **benchmark provenance** — the benchmark's name and a
  version or content hash — so a score is reproducible and attributable (Principle VI). Re-running the
  harness on the same version + benchmark MUST yield the same metric.
- **FR-102**: The harness MUST be **light** — primary metrics computed with small libraries (`jiwer`,
  `sacrebleu`/`rouge`, scikit-learn metrics) — and MUST NOT introduce a heavy eval framework or an
  always-on service (Principle III). It runs as gateway-side code and/or a one-shot script, not a resident
  worker.
- **FR-103**: The platform MUST expose a **promotion gate** that, before the `@serving` alias is moved to a
  candidate, retrieves the candidate's logged eval metric and the current incumbent's, and computes whether
  the candidate **regresses** beyond a configurable tolerance. The gate MUST respect each metric's
  **direction** (higher-is-better vs lower-is-better) and compare **like-for-like** (same modality + primary
  metric); it MUST refuse to judge a cross-modality / mismatched comparison.
- **FR-104**: The gate's **default** is **hard-gate with a small regression tolerance** (refuse a candidate
  that regresses beyond tolerance), with an explicit operator **override** flag that bypasses a block
  (promotion proceeds, flagged). The gate MUST remain **configurable** down to **warn-on-regression** (allow
  but flag), plus an explicit **missing-metric policy** (block vs warn when a candidate or incumbent has no
  logged metric) and an explicit **no-incumbent** behavior (pass, since there is nothing to regress
  against). Defaults are captured in plan.md; the mode + tolerance + override are operator-settable.
- **FR-105**: The gate MUST integrate into the **existing `registry.promote` path** in
  `gateway/app/registry.py` (the alias move) so that *every* promotion is gated — there is no ungated
  back-door — and the gate **verdict** (pass / warn / blocked, with candidate vs incumbent metric + delta +
  tolerance + mode) MUST be returned by the promote API and **surfaced in the Models/Runs UI**.
- **FR-106**: The platform MUST provide an **offline champion-challenger comparison**: score the current
  `@serving` champion and a challenger version on the same **held-out benchmark** (the default and only 011
  source), producing a per-metric comparison with a declared winner. It MUST load champion and challenger
  **sequentially** (one model in VRAM at a time, Principle II) and surface the comparison in the UI/CLI.
  **Shadow-replay** of logged inference requests is **deferred** to a **013-dependent** follow-on (it needs
  013's prediction + label logging to be meaningful) — out of scope for 011.
- **FR-107**: All 011 work MUST reuse the **existing MLflow** (tracking + alias registry) and gateway — no
  new datastore, broker, or always-on service. New code + small metric libs only; the eval metrics live in
  MLflow next to the model versions and runs they describe.

### Key Entities *(include if feature involves data)*

- **EvalResult**: a primary metric value for one (model version, benchmark) pair, logged to MLflow with the
  benchmark identifier (name + version/hash) and the metric's direction.
- **Benchmark**: a held-out, versioned dataset per modality (name + version/hash) the harness scores
  against; its identity is recorded with every EvalResult for reproducibility.
- **GateVerdict**: the outcome of a gated promotion — `pass` / `warn` / `blocked`, carrying candidate
  metric, incumbent metric, delta, tolerance, and the mode that produced it.
- **ChampionChallenger**: a per-metric, offline comparison of the `@serving` champion vs a challenger
  version on the **held-out benchmark**, with a declared winner; never two models resident at once.
  (Shadow-replay deferred to a 013-dependent follow-on.)

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-064**: For every supported modality, the harness computes the modality's primary metric on a held-out
  benchmark and logs it to MLflow against the model version, with the benchmark identifier recorded; a
  re-run yields the same score (reproducible).
- **SC-065**: A candidate that regresses beyond tolerance is **refused** in the **default hard-gate** mode
  (the `@serving` alias does not move), **promotable-but-flagged** with an explicit **override**, and
  **flagged** in warn mode (the alias moves, the event is marked); a non-regressing candidate promotes
  cleanly in all modes.
- **SC-066**: **Every** promotion path goes through the gate — there is no ungated way to move the
  `@serving` alias; the gate verdict (with candidate vs incumbent metric, delta, threshold, mode) is
  returned by the promote API.
- **SC-067**: The Models/Runs UI shows each version's eval metric and, for a promotion, the gate verdict
  (pass / warn / blocked).
- **SC-068**: The champion-challenger comparison produces a per-metric table with a declared winner
  (held-out benchmark) and is surfaced, with the VRAM mutex never violated (champion and challenger scored
  sequentially — at most one model in VRAM at any instant).
- **SC-069**: 011 adds no always-on service and no heavy framework — the gateway image stays light
  (Principle III); the eval metrics + gate live entirely on the existing MLflow + gateway, and the full
  001–007 suite still passes (no regression to serving, registry, datasets, training, drift, or tracing).

## Assumptions

- **MLflow is the metric home** — eval metrics log to MLflow against the model version / its run, reusing
  the tracking backend already in the stack (no new store). The alias-based registry (since 004) is the
  promotion mechanism the gate hooks into.
- **One model in VRAM is absolute** — there is no online A/B; champion-challenger is offline by
  construction (sequential held-out load; shadow-replay deferred → 013). This is a *feature* of the design,
  not a limitation to work around.
- **Benchmarks are small and held-out** — the harness targets *light*, representative held-out sets sized
  for the hardware profile (Principle III), not full research benchmarks; the point is a comparable,
  reproducible signal, not a leaderboard score.
- **Metric direction is part of the contract** — each primary metric declares higher-is-better vs
  lower-is-better so "regression" is well-defined; the gate compares like-for-like only.
- **The gate is mandatory, the mode is configurable** — promotion always consults the gate; whether a
  regression blocks or merely warns (and the missing-metric policy) is operator configuration, not a
  bypass.

## Non-Goals

- **Online A/B testing / live traffic split** — forbidden by Principle II (one model in VRAM); 011 is
  offline-only. Out of scope, not deferred.
- **Shadow-replay champion-challenger** — replaying logged inference requests through a challenger is
  **deferred to a 013-dependent follow-on**: it needs 013's prediction + label logging to score replayed
  requests meaningfully. 011's champion-challenger is held-out-benchmark only.
- **Hyperparameter optimization** — 011 *provides the target* (a tracked eval metric) for HPO; running HPO
  is increment 012.
- **A heavyweight eval framework** (e.g. full lm-eval-harness, large benchmark suites) — 011 stays light
  (Principle III) with small metric libs; a richer eval suite is a possible later increment.
- **Auto-promotion / auto-retraining policy** — 011 *gates a human-initiated promotion*; wiring the gate
  into an automated drift→retrain→promote loop is a separate increment.
- **New modalities beyond the existing serving surface** — the harness covers the modalities the platform
  already serves (LLM, vision) plus guidance for ASR/embeddings/tabular; it does not add a new serving
  modality.
