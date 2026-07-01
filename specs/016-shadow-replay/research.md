# Phase 0 Research: Shadow-Replay (016)

Design resolved in a grilling session (2026-06-30). Decision / Rationale / Alternatives. No blocking
`NEEDS CLARIFICATION`.

## D1 — Scope = all modalities, but extend 013 input capture first

- **Decision**: Shadow-replay targets all trainable modalities, but 016 **first extends 013's input
  capture** so vision (images) + ASR (audio) inputs are recoverable.
- **Rationale**: Codebase check — 013 logs the **full prompt** for LLM (`input_ref = req.prompt`,
  replayable) but only a **SHA hash** for vision and nothing replayable for ASR. Without recoverable
  inputs there is no replay corpus for those modalities.
- **Alternatives**: LLM-only (rejected — operator wants full coverage); reference-based re-fetch (rejected
  — arbitrary live prompts/images don't live in a retrievable store).

## D2 — Capture policy = sampled + capped + TTL, opt-in

- **Decision**: Capture recoverable inputs for a **bounded sample** (sampling rate and/or ring-buffer cap
  of the last N per modality) with a **retention TTL**, behind the existing `QUALITY_CAPTURE_IO` opt-in.
  Replay set = **captured ∩ labeled**.
- **Rationale**: Images/audio are heavy on the constrained drive (Principle III); ground-truth labels are
  sparse + delayed, so most captures never get labeled — capturing everything wastes storage. Opt-in keeps
  the privacy default (mirrors `MLFLOW_TRACE_CAPTURE_IO`).
- **Alternatives**: Capture-all + TTL prune (rejected — storage-heavy, mostly wasted); capture-only-when-
  label-likely (rejected — no upstream "will be labeled" signal exists).

## D3 — Challenger scoring reuses 015's in-process scorers; champion from logs

- **Decision**: Score the challenger by **reusing 015's in-process per-modality scorers** with the **replay
  corpus** as the row source; the challenger's served artifact is loaded **sequentially under the single
  GPU lease**. The **champion is not re-run** — 013 already logged its predictions on those exact requests,
  so champion-quality = read logged predictions + labels.
- **Rationale**: Makes 016 a thin follow-on to 015 (same scoring machinery, different input source);
  consistent with 015's guard-not-load + lease decisions; the champion's logged predictions ARE what
  production actually served (the truest champion signal).
- **Alternatives**: Re-run both (rejected — doubles GPU work, discards 013's logged data); gateway
  per-version loading (rejected — conflicts with 015's guard-not-load).

## D4 — Verdict is advisory, not gating

- **Decision**: Shadow-replay produces its own champion-vs-challenger verdict on production traffic,
  surfaced to the operator — **NOT** wired into the hard promotion gate (011/015).
- **Rationale**: Production labels are sparse + delayed; a hard gate on them would block promotions whenever
  labels are thin or a window hasn't accumulated. The held-out gate stays deterministic + always-available;
  shadow-replay is the informative second opinion. Keeps 011's single gate choke-point clean.
- **Alternatives**: Feed the gate (rejected — couples promotion to label availability + window
  accumulation; complicates the single choke-point).

## D5 — Exposure = on-demand endpoint → async trainer job

- **Decision**: `POST /models/{name}/shadow-replay {challenger}` dispatches an **async trainer-side job**
  (load challenger → score over the captured∩labeled window → read champion's logged quality → persist a
  verdict), returns a job id; a `GET` returns the verdict. Mirrors 014's `/batch` async pattern.
- **Rationale**: Replay loads + runs the challenger (a trainer-side GPU job under the lease), unlike
  011's sync gateway-side `/compare`. On-demand fits the advisory role + the on-demand serving ethos.
- **Alternatives**: A flag on `/compare` (rejected — mixes a sync gateway read with an async GPU job);
  scheduled/periodic (rejected — no scheduler; spends GPU without an operator ask).

## Open items (plan/tasks, non-blocking)

- Capture storage layout: a new `inputs/` prefix keyed by `prediction_id` vs extending the `predictions/`
  record; recoverable-input shape per modality (raw bytes vs base64 in the record).
- Sampling primitive (rate vs ring-buffer cap vs both) + TTL prune mechanism (lazy-on-write vs a sweep).
- Verdict persistence: `results` `shadow/` prefix vs an MLflow run (or both).
