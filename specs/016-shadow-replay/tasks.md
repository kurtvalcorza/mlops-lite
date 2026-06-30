# Tasks: Shadow-Replay Champion-Challenger

**Input**: Design documents from `specs/016-shadow-replay/` (spec.md, plan.md, research.md, data-model.md,
contracts/shadow-replay-endpoint.md, contracts/capture-extension.md, quickstart.md).

> **Status (2026-06-30):** **PLANNED — not started.** Grilled spec + plan complete. IDs continue the
> shared space (T303+). **Build sequenced AFTER 015** (US2 reuses `training/scoring/`); the US1 capture
> extension can proceed in parallel. Tests included (project pattern: importlib-load + injected scorer/
> store fakes, plus on-hardware SCs).

## Format: `[ID] [P?] [Story] Description`

- **[P]** = parallelizable (different files, no dependency). `[USx]` maps to the spec's user stories.

---

## Phase 1: Setup — config knobs (D2)

- [ ] **T303** Add the capture-policy config to `.env.example` + `gateway/app/quality.py` reads:
  `SHADOW_CAPTURE_SAMPLE` (rate), `SHADOW_CAPTURE_CAP_N` (per-modality ring-buffer cap),
  `SHADOW_CAPTURE_TTL_S` (retention), all behind the existing `QUALITY_CAPTURE_IO` opt-in. Document the
  privacy + Principle-III rationale.

---

## Phase 2: Foundational — capture storage primitives (BLOCKS US1)

- [ ] **T304** Pure policy functions in `gateway/app/quality.py`: `should_capture(modality)` (sampling +
  cap admission) and a TTL/cap **prune** helper — unit-testable without I/O.
- [ ] **T305** Capture storage: write a **recoverable input** record under a new `inputs/` prefix keyed by
  `prediction_id` (alongside `predictions/`/`labels/`), reusing 013's fire-and-forget + fail-open path
  (FR-146) and the `_log_sem` backpressure. Lazy prune (cap/TTL) on write.

**Checkpoint**: bounded, opt-in, recoverable-input capture exists and never affects serving.

---

## Phase 3: User Story 1 — bounded, replayable input capture (P1) 🎯 foundational

**Goal**: recoverable inputs (prompt/image/audio) captured per the sampled+capped+TTL policy (FR-146/147).

**Independent Test**: with capture on, serve per-modality requests → bounded recoverable inputs stored;
capture off → none; serving latency unaffected.

- [ ] **T306** [P] [US1] `tests/test_capture_policy.py` — sampling/cap admission + TTL prune (pure fns);
  opt-in off ⇒ nothing stored; bound respected.
- [ ] **T307** [US1] Wire the **recoverable input** into `gateway/app/routers/infer.py` + `stream.py`
  (prompt — already passed; ensure it routes to the new capture under the policy).
- [ ] **T308** [P] [US1] Wire `gateway/app/routers/vision.py` to capture the **image** (b64/bytes) — was a
  SHA hash only — under the policy.
- [ ] **T309** [P] [US1] Wire `gateway/app/routers/transcribe.py` to capture the **audio** under the policy.
- [ ] **T310** [US1] On-hardware: capture on → bounded recoverable inputs per modality (cap/TTL enforced);
  capture off → none; serving latency unchanged (**SC-094**).

**Checkpoint**: the replay corpus can accumulate.

---

## Phase 4: User Story 2 — shadow-replay a challenger → advisory verdict (P1)

**Goal**: load the challenger under the lease, score it over the captured∩labeled window (015 scorer),
compare to the champion's logged quality, persist an advisory verdict (FR-148/149/150/151).

**Independent Test**: with a labeled+captured window, `POST .../shadow-replay {challenger}` → a verdict vs
champion-logged on the same window; one model in VRAM; gate unchanged.

> **Depends on 015** (`training/scoring/` per-modality scorers).

- [ ] **T311** [P] [US2] `tests/test_shadow_window.py` — `captured ∩ labeled` window resolution +
  newest-`WINDOW_N` selection (injected store).
- [ ] **T312** [P] [US2] `tests/test_shadow_verdict.py` — advisory verdict math: challenger-replay vs
  champion-logged, like-for-like metric/direction, `advisory:true`, never gates.
- [ ] **T313** [US2] `gateway/app/shadow.py` (NEW): resolve the replay window (captured∩labeled), read the
  **champion's logged quality** on it (013, no re-run — FR-149), dispatch the trainer job, persist/read the
  verdict (`results` `shadow/` prefix and/or MLflow).
- [ ] **T314** [US2] `training/flows/shadow_replay.py` (NEW): trainer-side job — **acquire the lease**, load
  the challenger's served artifact, score over the replay corpus via **015's scorer** (replay rows as the
  source), release; return per-metric value. **One model in VRAM** (FR-148).
- [ ] **T315** [US2] `gateway/app/routers/models.py`: `POST /models/{name}/shadow-replay` (dispatch, `202`)
  + `GET /models/{name}/shadow-replay/{id}` (verdict) per contracts/shadow-replay-endpoint.md; BFF
  allowlist if surfaced in the UI.
- [ ] **T316** [US2] On-hardware: replay a challenger → advisory verdict vs champion-logged on the same
  window; `nvidia-smi` one-model; **gate unchanged** (**SC-095, SC-096, SC-097**).

**Checkpoint**: production-traffic champion-challenger works, advisory.

---

## Phase 5: User Story 3 — graceful insufficient/no-corpus (P2)

**Goal**: honest degradation when labels/captures are sparse (FR-152).

**Independent Test**: `< MIN_PAIRS` captured∩labeled → `insufficient_data`; capture off → `no_corpus`.

- [ ] **T317** [P] [US3] `tests/test_shadow_insufficient.py` — `< MIN_PAIRS` ⇒ insufficient_data; capture
  disabled ⇒ no_corpus; modality with no captured inputs ⇒ clear refusal.
- [ ] **T318** [US3] Implement the guards in `gateway/app/shadow.py` + the endpoint (FR-152) per the
  contract's response table.
- [ ] **T319** [US3] On-hardware: insufficient + no-corpus paths return explicit statuses, never a verdict
  (**SC-098**).

**Checkpoint**: no misleading verdicts from thin data.

---

## Phase 6: Polish & cross-cutting

- [ ] **T320** [P] Docs: README (shadow-replay + the capture toggles), `monitoring/README.md` (the
  production-traffic comparison alongside 013 quality); flip spec/plan/tasks Status → BUILT.
- [ ] **T321** No-regression: full **001–015** suite green (GPU-tenant tests in isolation) (**SC-099**).
- [ ] **T322** [P] Confirm **no new dependency** (reuses 013 logging + 015 scorers + 011 metrics); update
  the deps note. Verify the gate (011) is byte-for-byte unchanged.
- [ ] **T323** PR + **dual-bot review loop** (`@claude` + `@codex`) → fix → merge when clean.

---

## Dependencies & Execution Order

- **Phase 1 (config)** → no deps.
- **Phase 2 (capture primitives)** → after Phase 1; **BLOCKS US1**.
- **US1 (Phase 3)** → after Phase 2. Independent of 015 — can land before 015 merges.
- **US2 (Phase 4)** → after US1 (needs the corpus) **and after 015 merges** (reuses `training/scoring/`).
- **US3 (Phase 5)** → after US2 (guards on the same endpoint).
- **Polish (Phase 6)** → after the user stories.

### Parallel opportunities

- T307–T309 (per-modality capture wiring) in parallel; test files T306/T311/T312/T317 in parallel with
  their targets stubbed.

## Notes

- Capture is **fire-and-forget + fail-open** — never affect serving (FR-119/FR-146). Keep it **bounded**
  (sample+cap+TTL) and **opt-in** (privacy).
- The challenger load is **sequential under the single lease** (Principle II); the champion is **not**
  re-run (logged predictions, FR-149).
- The verdict is **advisory** — do **not** touch 011's single gate choke-point (FR-150/SC-097).
- Validate GPU-tenant tests **in isolation** (the single lease serializes them — expected).
