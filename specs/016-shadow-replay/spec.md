# Feature Specification: Shadow-Replay Champion-Challenger (production-traffic evaluation)

**Feature Branch**: `016-shadow-replay`

**Created**: 2026-06-30

**Status**: **BUILT (offline, 2026-06-30) — on-hardware SCs (SC-094/095/096/098) pending the RTX 5070 Ti box.** Code + offline unit tests landed and green (no regression); advisory verdict never gates.

**Input**: The deferred half of 011's champion-challenger. 011 compares champion vs challenger on a fixed
**held-out benchmark**; it explicitly deferred **shadow-replay** — re-scoring a challenger against
**logged production inference requests** — to "a 013-dependent follow-on" because it needs prediction +
ground-truth-label logging to be meaningful. **013 (quality monitoring) is now built** (it logs served
predictions with a stable id + model version + input, attaches delayed labels by id, and computes windowed
per-modality quality), so shadow-replay is unblocked. The value over the held-out compare: judge a
challenger on the **real traffic distribution** the model actually sees, not a curated smoke set.

> **Grilled decisions (2026-06-30):**
> 1. **Scope = the labeled-prediction modalities (LLM / vision / ASR), and extend 013 input capture
>    first.** Shadow-replay needs a clean per-prediction `(input, label)` pair, so it applies to
>    text-generation, image-classification, and ASR — the modalities 013 logs a single prediction +
>    ground-truth label for. **Embeddings and tabular are OUT of scope**: embeddings serving is recall over
>    a retrieval *set* (no single per-request label) and tabular has no 013 prediction logging — neither
>    fits the per-prediction replay shape (a different formulation would be its own increment). Today 013
>    logs the **full prompt** for LLM (replayable) but only a **SHA hash** for vision (not replayable) and
>    nothing replayable for ASR — so 016 first **extends 013's input capture** so vision (images) + ASR
>    (audio) inputs are recoverable, then shadow-replays those three modalities.
> 2. **Capture policy = sampled + capped + TTL, configurable, behind the existing `QUALITY_CAPTURE_IO`
>    opt-in.** Capture inputs for a bounded sample (a sampling rate and/or a ring-buffer cap of the last N
>    per modality) with a retention TTL — NOT every request — so image/audio storage stays bounded on the
>    constrained drive (Principle III). The replay set = **captured ∩ labeled** pairs.
> 3. **Challenger scoring = reuse 015's in-process per-modality scorers**, fed the **replay corpus**
>    (logged inputs) instead of the held-out benchmark; the challenger's served artifact is loaded under
>    the single GPU lease, sequentially. **The champion needs no re-run** — 013 already logged its
>    predictions on those exact requests, so champion-quality = read logged predictions + labels.
> 4. **Verdict = advisory, operator-weighed.** Shadow-replay produces its own champion-vs-challenger
>    verdict on production traffic, surfaced to the operator — **NOT** wired into the hard promotion gate.
>    The 011/015 held-out gate stays the deterministic, always-available promotion signal; shadow-replay is
>    the informative "...and here's how it'd do on real traffic" second opinion (production labels are
>    sparse/delayed, so gating on them would stall promotions).
> 5. **Exposure = a new on-demand endpoint → async trainer job.** `POST /models/{name}/shadow-replay`
>    `{challenger}` dispatches a trainer-side job (load challenger → score over the captured∩labeled replay
>    window → read champion's logged quality on the same window → persist a verdict), returns a job id; a
>    `GET` returns the verdict. Mirrors 014's `/batch` async pattern. (Not a flag on `/compare` — that path
>    is a sync gateway-side logged-metric read; not scheduled — the platform has no scheduler and serving is
>    on-demand.)

> **Scope note**: 016 **advances Principle VI (Reproducibility & Observability)** — it evaluates on the
> real served distribution and records the verdict. It adds **no new always-on service, no new runtime**:
> input capture is an extension of 013's existing fire-and-forget logging; the challenger scoring is a
> trainer-side job reusing **015's** in-process scorers; the verdict rides the existing MinIO `results`
> bucket / MLflow. Requirement IDs continue the shared space (FR-146+, SC-094+, tasks T303+). **No
> constitution amendment** (no new stage; advisory eval, not a new gate). See plan.md → Constitution Check.

> **Hard boundary (NON-NEGOTIABLE)**: **Principle II — one GPU tenant under the single race-free lease
> (008, v1.4.0)**: the challenger is loaded + scored **sequentially under the lease**, never co-resident.
> The **frozen Blackwell sm_120 GPU stack** is **NOT** touched and **no heavy new dependency** is added
> (Principle III) — reuses 013's logging, 015's scorers, and 011's pure-Python metrics. Input capture
> stays **behind the `QUALITY_CAPTURE_IO` opt-in** (privacy) and **bounded** (sampled+capped+TTL).

> **Dependencies**: **015** (reuses its in-process per-modality scorers — 016 builds after 015 merges) ·
> **013** (extends its prediction/label logging) · **011** (the compare/verdict shape it complements).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Bounded, replayable input capture (Priority: P1, foundational)

013's prediction logging is extended so that, behind the `QUALITY_CAPTURE_IO` opt-in and within a bounded
**sampled + capped + TTL** policy, the **recoverable input** is captured for each sampled prediction
(prompt for LLM, image for vision, audio for ASR) — so a challenger can later be re-run over the exact
inputs the champion served. Storage stays bounded on the constrained drive.

**Why this priority**: Without recoverable inputs there is no replay corpus. It is the foundation the
comparison runs on, and it carries the storage/privacy guardrails — so it leads.

**Independent Test**: With capture on, serve a handful of requests per modality; confirm the sampled
inputs are stored recoverably (image/audio/prompt), the cap + TTL bound the count, and capture-off stores
nothing extra. Confirm serving latency is unaffected (fire-and-forget, like 013).

**Acceptance Scenarios**:

1. **Given** `QUALITY_CAPTURE_IO` on + a sampling/cap policy, **When** predictions are served, **Then** a
   bounded sample of recoverable inputs is stored (older ones pruned by cap/TTL), and serving is unaffected.
2. **Given** `QUALITY_CAPTURE_IO` off, **When** predictions are served, **Then** no recoverable input is
   stored (privacy default preserved).

---

### User Story 2 — Shadow-replay a challenger and get an advisory verdict (Priority: P1)

An operator requests a shadow-replay of a challenger version. A trainer-side job loads the challenger,
scores it over the **captured ∩ labeled** replay window (reusing 015's in-process scorer for the modality),
reads the **champion's logged quality** on the same window, and persists an **advisory** champion-vs-
challenger verdict (per-metric, honouring direction) — never two models resident, never blocking the gate.

**Why this priority**: This is the payoff — the production-traffic comparison 011 deferred. It depends on
US1's corpus and 015's scorers.

**Independent Test**: With a labeled replay window present, `POST /models/{name}/shadow-replay {challenger}`
→ a job runs, returns a verdict comparing challenger-on-replay vs champion-logged-quality on the same
window; `nvidia-smi` shows ≤ one model resident; the promotion gate is unchanged.

**Acceptance Scenarios**:

1. **Given** a labeled+captured replay window and a challenger, **When** shadow-replay runs, **Then** it
   loads the challenger under the lease, scores it over the window, and returns a verdict vs the champion's
   logged quality on the same `(input, label)` pairs — like-for-like (same modality metric + direction).
2. **Given** the champion's predictions already logged (013), **When** shadow-replay runs, **Then** the
   champion is **not** re-run — its quality comes from logged predictions + labels.
3. **Given** a shadow-replay verdict, **When** an operator promotes, **Then** the **hard gate** (011/015,
   held-out) is unchanged — the verdict is advisory only.

---

### User Story 3 — Insufficient/sparse data is handled gracefully (Priority: P2)

When the captured∩labeled window is too small (labels are sparse/delayed), shadow-replay reports
**insufficient data** (like 013's `MIN_PAIRS`) rather than a misleading verdict from a handful of pairs.

**Why this priority**: Production labels are sparse/delayed; the common early state is "not enough yet."
The feature must degrade honestly. Smaller than US1/US2 but essential for trust.

**Independent Test**: Request a shadow-replay when fewer than the minimum labeled+captured pairs exist →
an explicit "insufficient data" result, not a verdict.

**Acceptance Scenarios**:

1. **Given** fewer than `MIN_PAIRS` captured∩labeled pairs, **When** shadow-replay runs, **Then** it
   returns an explicit insufficient-data status (no verdict computed).

---

### Edge Cases

- **Captured but unlabeled** / **labeled but not captured**: only the **intersection** is replayable; the
  rest is excluded from the window (reported in the result's denominator).
- **TTL/cap eviction races label arrival**: a label arriving after its input was pruned simply isn't
  replayable — counted as excluded, not an error.
- **Challenger == champion (`@serving`)**: degenerate (verdict ≈ tie); allow but note it.
- **Privacy**: with `QUALITY_CAPTURE_IO` off there is no replay corpus → shadow-replay reports "capture
  disabled / no corpus" rather than silently scoring nothing.
- **Modality without finalized capture** (until US1 lands per modality): shadow-replay refuses with a clear
  "inputs not captured for <modality>" message.
- **One model in VRAM**: challenger load is sequential under the lease; the job releases promptly.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-146**: 013 prediction logging MUST be extended to capture the **recoverable input** (prompt / image /
  audio) per sampled prediction, **behind `QUALITY_CAPTURE_IO`**, under a **sampled + capped + TTL** policy
  that bounds storage (Principle III). Serving MUST stay unaffected (fire-and-forget, fail-open — FR-119).
- **FR-147**: The capture sampling rate, per-modality cap, and TTL MUST be **operator-configurable**.
- **FR-148**: Shadow-replay MUST score the challenger over the **captured ∩ labeled** replay window using
  **015's in-process per-modality scorer**, with the challenger's served artifact loaded **sequentially
  under the single GPU lease** (Principle II) — never co-resident with another model.
- **FR-149**: The champion side MUST use its **already-logged predictions** (013) on the same window — no
  champion re-run.
- **FR-150**: The verdict MUST be **like-for-like** (same modality metric + direction, same `(input,label)`
  window) and **advisory** — it MUST NOT alter the 011/015 hard promotion gate (FR-105 single choke-point
  preserved).
- **FR-151**: Shadow-replay MUST be exposed as **`POST /models/{name}/shadow-replay`** (challenger version)
  dispatching an **async trainer-side job**, with a **`GET`** for the persisted verdict. Verdicts ride the
  existing MinIO `results` bucket (and/or MLflow).
- **FR-152**: With too few captured∩labeled pairs (`< MIN_PAIRS`), shadow-replay MUST report **insufficient
  data**, not a verdict. With capture disabled, it MUST report **no corpus**.
- **FR-153**: 016 MUST add **no new always-on service, no new runtime, and no heavy dependency**, MUST NOT
  move the frozen GPU stack, and MUST leave the held-out compare (011) + score-at-registration (015) intact.

> **[OPEN — for plan/tasks]** Exact capture storage layout under the `results` bucket (a new `inputs/`
> prefix keyed by prediction_id, vs. extending the existing `predictions/` record); the sampling primitive
> (rate vs ring-buffer cap vs both) and TTL prune mechanism (lazy on write vs a sweep).

### Key Entities

- **Captured input**: the recoverable served input (prompt/image/audio bytes or a retrievable ref) for a
  sampled prediction, keyed by `prediction_id`, bounded by sample/cap/TTL.
- **Replay window**: the `captured ∩ labeled` `(input, label)` pairs for a model+modality over the recent
  window — the corpus both sides are scored on.
- **Shadow-replay verdict**: per-metric champion (logged) vs challenger (replayed) on the window, with the
  metric/direction, pair count, and an advisory winner — persisted, not gating.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-094**: With capture on, a bounded sample of **recoverable** inputs is stored per modality (cap + TTL
  enforced); with capture off, none — and serving latency is unaffected.
- **SC-095**: A shadow-replay over a labeled+captured window returns an advisory verdict comparing the
  challenger (replayed) vs the champion (logged) on the **same** `(input, label)` pairs, honouring the
  modality metric + direction.
- **SC-096**: During a shadow-replay, **at most one model is resident in VRAM** at any instant (lease
  serialized; `nvidia-smi` confirms).
- **SC-097**: The promotion gate (011/015) is **unchanged** — shadow-replay never blocks or alters a
  promotion (it is advisory).
- **SC-098**: With `< MIN_PAIRS` captured∩labeled pairs, shadow-replay reports **insufficient data**; with
  capture disabled, **no corpus** — never a misleading verdict.
- **SC-099**: The frozen GPU stack + dependency footprint are unchanged; 001–015 suites still green.

## Assumptions

- 015 is merged first (016 reuses its in-process per-modality scorers); 013's logging + MinIO `results`
  bucket are the substrate for captured inputs + labels.
- Production ground-truth labels are sparse + delayed → the replay window is small and the common early
  state is "insufficient data" (handled by US3).
- The single GPU lease (008) is the sole VRAM-admission point; the challenger load runs under it.
- Capturing recoverable inputs is acceptable **only behind the opt-in + bounded policy** (privacy + the
  constrained drive).

## Non-Goals

- **No hard-gating on production labels** (advisory only — grilled decision 4).
- **No online A/B / live traffic split** (Principle II).
- **No champion re-run** (uses logged predictions).
- **No scheduler / automatic periodic replay** (on-demand only).
- **No unbounded input capture** (sampled + capped + TTL, opt-in).
- **No embeddings / tabular shadow-replay** — they lack a clean per-prediction `(input, label)` pair
  (embeddings = recall over a set; tabular has no 013 prediction logging). A separate formulation would be
  its own increment.
- **No change to the held-out compare (011) or score-at-registration (015)** — 016 complements them.
