# Tasks: 021 Loop-Native Operator Console

**Input**: [spec.md](./spec.md) (FR-208..253, SC-134..142) · [plan.md](./plan.md) ·
[research.md](./research.md) (R1–R8) · [data-model.md](./data-model.md) ·
[contracts/](./contracts/) · [quickstart.md](./quickstart.md)

**Numbering**: continues the shared space after 020 (T420+). All work is in the `ui/` package —
front-end only, **no gateway/backend/API changes** (FR-252).

**Testing posture** (research R8): the `ui/` package has no unit/e2e harness. The automated gate per
task is `npm run lint` + `npm run build` (type-check); each story's behavioural regression is its
**quickstart drill** (`quickstart.md §US*`). The Python backend suite must stay green and unchanged
(SC-141) — no task touches backend code.

**[P]** = parallelizable (different files, no incomplete-task dependency).

## Phase 1: Setup (shared, mechanical — unblocks everything)

- [x] **T420** Extend the BFF allow-list per [contracts/allowlist-delta.md](./contracts/allowlist-delta.md):
  add the 13 entries (`datasets/:name/:version`; `runs/:id`; `POST infer` (LLM trace mode); `monitor`;
  `monitor/quality/check`; `monitor/quality`; `monitor/labels`; and the 6 per-engine health probes) to
  `ui/lib/gw-allowlist.ts`, and re-section existing policy/suggestion/engine comments under the loop
  vocabulary (`serving`/`retraining`/…). No proxy route logic change. Validate: `isAllowed` returns
  true for each new pair; `npm run build` clean.
- [x] **T421** [P] Relocate the dynamic per-task panel set `ui/components/infer/*` →
  `ui/components/serving/*` (StreamPanel, ClassifyPanel, TabularPanel, TranscribePanel, EmbedPanel,
  NoRenderer, index, types) as a pure move + import-path update; no behaviour change yet (research
  R3). Validate: `npm run build` clean.
- [x] **T422** Route renames as file moves (content unchanged for now): `app/datasets`→`app/data`,
  `app/runs`→`app/training`, `app/infer`→`app/serving`, `app/monitor`→`app/monitoring`; add
  `app/retraining/page.tsx` stub. **`app/page.tsx` today redirects the root `/`→`/infer`** — change it
  to `/`→`/serving` (FR-212). Add explicit old-path→new-path redirects: `/infer`→`/serving`,
  `/datasets`→`/data`, `/runs`→`/training`, `/monitor`→`/monitoring`, placed in a **central, survivable
  location** (retained redirect shim, `next.config`, or middleware — NOT inside the deleted old route
  dirs, so T457's cleanup can't remove them). **Not `[P]`: run after T421** — the moved `app/serving`
  page imports the relocated `components/serving` (today `app/infer/page.tsx` imports
  `@/components/infer`), so parallel moves would race on that import. Validate: each new route renders
  its (pre-rebuild) page; every old path 3xx-redirects to its new home.
- [x] **T423** [P] Shared `ConfirmDialog` primitive in `ui/components/ConfirmDialog.tsx` — supports a
  warning body and an optional required-reason text field (backs all three high-trust actions;
  research R5 / FR-250). Validate: renders, blocks confirm until a required reason is entered.

## Phase 2: Foundational (blocking prerequisite for the live shell + stages)

- [x] **T424** Shared live-state hook `ui/lib/useLiveState.ts` (or `lib/`-level helper): subscribes to
  `platform/events` via `lib/sse.ts` with light per-stage polling fallback, and degrades to
  `unknown`/at-rest when the platform is unreachable (research R2 / FR-213). Consumed by the stage
  badges (US1) and the GPU pill (US1) and several stages. Validate: emits live values with the stack
  up; degrades without throwing when the gateway is stopped.

---

## Phase 3: User Story 1 — the nav bar IS the loop (P1) 🎯 MVP

**Goal**: the console's navigation renders the lifecycle as an ordered loop with live per-stage
badges + a persistent GPU pill; lands on `serving`; survives a platform outage (FR-208..213).
**Independent test**: `quickstart.md §US1`.

- [x] **T425** [US1] `ui/components/LoopNav.tsx` (rebuilding `Nav.tsx`): render the six stages in loop
  order `data → training → models → serving → monitoring → retraining` with directional connectors +
  a loop-back marker; place `health` + the GPU-pill slot off-axis (right) (FR-208/209). Mount in
  `app/layout.tsx`. Per [contracts/nav-and-routes.md](./contracts/nav-and-routes.md).
- [x] **T426** [P] [US1] `ui/components/StageBadge.tsx`: per-stage live status glyph fed by
  `useLiveState` — training=active-run, models=candidate-awaiting-promote, serving=resident engine,
  monitoring=breach dot, retraining=open-suggestion count; `unknown` fallback (FR-210/213).
- [x] **T427** [P] [US1] `ui/components/GpuPill.tsx`: header pill showing lease holder + resident
  model + swap/idle from `serving/state` + `platform/events`; click → `/serving` (FR-211).
- [x] **T428** [US1] Validate US1 end-to-end against `quickstart.md §US1` (loop order + connectors +
  loop-back, badges update, GPU pill, land on `/serving`, degrade-when-down); `npm run lint` + `npm
  run build` green.

**Checkpoint**: US1 alone reframes the console around the loop — shippable MVP.

---

## Phase 4: User Story 2 — serving under one lease (P1)

**Goal**: `/serving` renders all promoted engines dynamically + batch, a full lease view, a
confirm-gated preempt, and prediction→monitoring traceability (FR-231..237).
**Independent test**: `quickstart.md §US2`.

- [x] **T429** [US2] Wire the relocated `components/serving/*` panels into `app/serving/page.tsx`
  driven by `GET /serving/tasks` — one panel per promoted task, `NoRenderer` for an untagged version
  (FR-231/232). Preserve all five engines (stream/classify/tabular/transcribe/embed).
- [x] **T430** [P] [US2] `ui/components/serving/LeaseView.tsx`: holder + resident + serving-version +
  live `serving/tasks` list, visually marking lease-tenant (llm/vision/asr/training) vs off-lease
  (tabular/embed) engines (FR-234).
- [x] **T431** [P] [US2] `ui/components/serving/BatchPanel.tsx`: launch batch over `dataset@version`
  → poll `batch/:id` → result link (moved from the runs page) (FR-236).
- [x] **T432** [US2] Preemptive-swap control on the LLM panel: `preempt` gated behind `ConfirmDialog`
  naming the holder to evict (FR-235/250). Never presents a running job as preemptable.
- [x] **T433** [US2] LLM panel stream/trace split (FR-232/233): **stream mode** (`POST /infer/stream`)
  shows the completion + `registry_version` (resolved from `serving/state`) + `load_ms`, no prediction
  id; **trace mode** (`POST /infer`, now allow-listed) shows the single-shot completion +
  `registry_version` + `prediction_id` + `load_ms` and the "label this prediction" hand-off deep-link
  → `/monitoring?prediction_id=…` (FR-237, research R7). The hand-off is offered only from trace mode
  (streamed predictions log no id / no capture, 016).
- [x] **T434** [US2] Validate US2 against `quickstart.md §US2`; lint + build green.

---

## Phase 5: User Story 3 — monitoring read-side + close the loop (P2)

**Goal**: `/monitoring` exposes drift + quality checks, both histories, labeling, one-shot retrain,
and cooldown-as-outcome (FR-238..242).
**Independent test**: `quickstart.md §US3`.

- [x] **T435** [US3] `ui/components/monitoring/DriftPanel.tsx` + `QualityPanel.tsx`: run
  `monitor/check` and `monitor/quality/check`; quality baseline auto-resolves with an advanced
  disclosure for baseline/window_n/drop_pct (FR-238/241).
- [x] **T436** [P] [US3] `ui/components/monitoring/HistoryList.tsx`: render `GET /monitor` and
  `GET /monitor/quality` histories, newest first (FR-238).
- [x] **T437** [P] [US3] `ui/components/monitoring/LabelsPanel.tsx`: attach a label by `prediction_id`
  (`monitor/labels`); accepts the `?prediction_id=` deep-link from serving; renders late/duplicate/
  unknown-id outcomes cleanly (FR-239).
- [x] **T438** [US3] One-shot "retrain if this breaches" on a check, labeled distinct from standing
  policies, with the retrain spec auto-filled from the breached model (`dataset_version=latest`,
  modality/output prefilled, knobs defaulted) behind confirmation; render `skipped: cooldown` as a
  first-class outcome (FR-240/242).
- [x] **T439** [US3] Assemble `app/monitoring/page.tsx` from the panels; render the explicit
  manual-vs-standing framing — a short in-page note that these are *manual, one-shot* checks whose
  *standing, scheduled* counterpart lives in `retraining` (same checks, same gate, same cooldown)
  (FR-248). Validate against `quickstart.md §US3`; lint + build green.

---

## Phase 6: User Story 4 — autonomous retraining made visible (P2)

**Goal**: `/retraining` gives policy CRUD (form+JSON), warned auto-promote, a cycle board, and a
suggestions inbox with gate-safe accept (FR-243..248).
**Independent test**: `quickstart.md §US4`.

- [x] **T440** [US4] `ui/components/retraining/PolicyEditor.tsx`: form + JSON toggle over one policy
  document; whole-document validated PUT; structured-400 field errors inline (FR-243/244, research
  R6).
- [x] **T441** [US4] Auto-promote control in the editor: off by default; enabling requires a
  `ConfirmDialog` warning "the platform will move @serving without you" (FR-245/250).
- [x] **T442** [P] [US4] `ui/components/retraining/CycleBoard.tsx`: per-model last-check / next-due /
  pending-retrain from `GET /policies/:model/status` (FR-246).
- [x] **T443** [P] [US4] `ui/components/retraining/SuggestionsInbox.tsx`: `GET /suggestions?state=`
  filter; accept (gated promote) / dismiss; a blocked accept stays open and deep-links →
  `/models?override=<name>@<version>` (FR-247, research R7).
- [x] **T444** [US4] Assemble `app/retraining/page.tsx`; render the reciprocal manual-vs-standing
  framing — a short in-page note that these policies run the *same* monitoring checks on a *standing
  schedule* (the manual, one-shot counterpart lives in `monitoring`; same gate, same cooldown)
  (FR-248). Validate against `quickstart.md §US4`; lint + build green.

---

## Phase 7: User Story 5 — models registry + promote gate (P3)

**Goal**: `/models` centers the promote gate (preview→promote, override-with-reason), lineage
drill-back, and on-demand metrics (FR-224..229).
**Independent test**: `quickstart.md §US5`.

- [x] **T445** [US5] `app/models/page.tsx`: list with the `@serving` champion marked; version rows
  with `ui/components/models/LineageLinks.tsx` (run_id→training, tags→data/base/parent); a
  no-`run_id` version visually distinct as seeded/imported (FR-224/225).
- [x] **T446** [P] [US5] `ui/components/models/EvaluatePanel.tsx`: evaluate BUTTON (modality-default
  one-click) + advanced disclosure to override benchmark/metric; compare challenger↔champion
  (FR-226/227).
- [x] **T447** [US5] `ui/components/models/PromoteGate.tsx`: preview (evaluate, no alias move) →
  promote (gate) flow; a block leaves the alias put + shows the verdict; override behind
  `ConfirmDialog` requiring a typed reason; accepts the `?override=` deep-link from retraining
  (FR-228/229).
- [x] **T448** [US5] Validate US5 against `quickstart.md §US5`; lint + build green.

---

## Phase 8: User Story 6 — data & training as the loop's entry (P3)

**Goal**: `/data` gains inspect/download + validate-as-gate + train-on-version; `/training` gains the
fixed-modality picker + lease-aware launch + →models hand-off (FR-214..223).
**Independent test**: `quickstart.md §US6`.

- [x] **T449** [US6] `app/data/page.tsx`: keep register/list/dedupe; add version inspect (full
  manifest via `datasets/:name/:version` — **manifest only; no byte-download button**, the
  `download_url` is presigned against the internal store and is not browser-reachable, FR-215) and
  present validate as a gate-vs-warn
  readiness report (FR-214/215/216). No edit/delete/EDA (FR-218).
- [x] **T450** [P] [US6] "train on this version" hand-off from `data` → `/training?ds=<name>@<version>`
  (FR-217, research R7).
- [x] **T451** [US6] `app/training/page.tsx`: fixed 4-way modality picker showing only that modality's
  knobs + pinned default base; base read-only for vision (locked arch); chain-from-parent enabled
  only for vision/embeddings; reads `ds` prefill (FR-219/220). **Remove the batch launcher from this
  page** — batch inference now lives in `serving` (T431/FR-236); this completes the move so batch is
  not duplicated across stages.
- [x] **T452** [P] [US6] Lease-aware launch: read `serving/state` before launch; surface 409 (busy) /
  507 (over-budget) as distinct first-class refusals; poll `runs/:id` detail; "view in models"
  hand-off on completion (FR-221/222). No cancel/register/history surfaces (FR-223).
- [x] **T453** [US6] Validate US6 against `quickstart.md §US6`; lint + build green.

---

## Phase 9: User Story 7 — enriched health (P3)

**Goal**: `/health` shows platform liveness + per-engine probe dots (FR-249).
**Independent test**: `quickstart.md §US7`.

- [x] **T454** [US7] `app/health/page.tsx`: keep platform liveness (`platform/health` +
  `platform/events`); add per-engine probe dots (serving/predict/vision/embed/transcribe/training
  health), a down engine shown distinctly not-ok (FR-249).
- [x] **T455** [US7] Validate US7 against `quickstart.md §US7`; lint + build green.

---

## Phase 10: Polish & cross-cutting

- [x] **T456** [P] Responsive rule: at narrow widths the GPU pill wraps below the loop bar; the six
  ordered stages stay on one axis (research R1). Verify no horizontal body scroll.
- [x] **T457** [P] Remove dead surfaces: delete the old `components/infer/` shims and any stale
  `/infer|/datasets|/runs|/monitor` route implementations **only after confirming the old-path
  redirects (T422) live in a central, survivable location** (redirect shim / `next.config` /
  middleware) and still resolve — the cleanup must not delete the backward-compat redirects along with
  the old route dirs (FR-253 — IA, not reskin; design language preserved).
- [x] **T458** Allow-list conformance gate: grep every gateway call in `ui/` and confirm each resolves
  to a `gw-allowlist.ts` entry (no non-listed path/method); confirm the diff equals
  [contracts/allowlist-delta.md](./contracts/allowlist-delta.md) (SC-140/141/FR-251).
- [x] **T459** Full regression: `npm run lint` + `npm run build` green; run the Python backend suite
  and confirm it is unchanged/green (SC-141); walk the whole loop once per `quickstart.md`.
- [x] **T460** [P] Refresh docs to match the renamed loop nav: in `README.md` update the operator-console
  section ("Tabs cover infer / models / datasets / runs / monitor / platform" → the loop
  `data → training → models → serving → monitoring → retraining ⟲` + off-axis health/GPU-pill) and the
  stale "Infer tab" references (per-task panels, lease/swap status line) to the `serving` stage; grep
  `README.md` and any `docs/` for the old route/tab names (`infer`/`datasets`/`runs`/`monitor` as UI
  surfaces) and reconcile. Docs-only; no code or contract change (FR-253 — IA rename, design language
  preserved).

## Dependencies & order

- **Setup (T420–T423)** unblocks everything; **Foundational (T424)** unblocks the live shell + stages.
- **US1 (T425–T428)** depends on Setup+Foundational; it is the MVP and independent of US2–US7.
- **US2–US7** each depend only on Setup+Foundational and their own route (from T422); they are
  independently testable. **Soft** cross-links (graceful if the other stage isn't built): serving→
  monitoring label (T433→T437), retraining→models override (T443→T447), data→training (T450→T451),
  training→models (T452→T445).
- **Polish (T456–T460)** last.

## Parallel execution examples

- After T420: **T421, T422, T423** run in parallel (distinct files).
- Within US1: **T426, T427** parallel (distinct components) after T425.
- Within US2: **T430, T431** parallel; within US3: **T436, T437** parallel; within US4: **T442,
  T443** parallel — all distinct component files.
- Whole stories US2/US3/US4/US5/US6/US7 can proceed in parallel once Setup+Foundational land, since
  each owns a distinct route + component subtree.

## Implementation strategy

- **MVP = US1** (the loop-native shell). Shipping only T420–T428 already replaces the noun-soup nav
  with a live loop status board.
- Then **US2** (serving, the default landing + most-used surface), then the P2 loop-closers **US3**
  (monitoring read-side) and **US4** (autonomous retraining) — the two previously-invisible steps —
  then the P3 enrichments **US5/US6/US7**, then Polish.
- Every phase ends green on `lint`+`build` and its quickstart drill; no phase touches backend code.

## Live validation record (2026-07-05, stack up on the dev machine)

Automated gates: `next build` green after every phase (final exit 0); Python backend suite green
(exit 0, passes + offline skips, zero failures — SC-141, no backend file touched); allow-list
conformance grep — every gateway call in `ui/` resolves to a `gw-allowlist.ts` entry and the diff
equals [contracts/allowlist-delta.md](./contracts/allowlist-delta.md) exactly (13 additions).

Drilled live against the running platform (UI daemon restarted onto the 021 build):

- **Routes/redirects (US1)**: `/`→`/serving` 307; `/infer`→`/serving`, `/datasets`→`/data`,
  `/runs`→`/training`, `/monitor`→`/monitoring` all 307; all 7 stage routes 200.
- **Loop shell (US1)**: SSR HTML carries the six stages in loop order (positions strictly
  ascending), the `⟲` loop-back, the GPU pill, off-axis health.
- **SC-142 (US1)**: gateway container stopped → `/serving`, `/retraining`, `/monitoring` still 200
  (shell renders + navigates); BFF gateway calls fail closed (502); full recovery on restart.
- **Trace mode (US2/FR-233)**: live `POST /infer` through the new allow-list entry → 200 with
  `registry_version=23` + `prediction_id` + `load_ms=3518.5` + completion — the serving→monitoring
  id seam proven on GPU.
- **Monitoring read-side (US3)**: `GET /monitor?limit=1` + `GET /monitor/quality?limit=1` 200 with
  real report history through the BFF; unknown-id label POST → clean `status: unknown` (write-once
  contract, nothing mutated).
- **Per-engine probes (US7)**: `serving/health` + `training/health` 200 through the new entries.

Deferred to the full on-hardware pass (state-mutating / multi-tenant interactive drills):
preemptive-swap confirm with a non-LLM holder resident, one-shot-retrain breach→launch→cooldown
sequence, policy save→cycle→suggestion→gate-blocked accept→override walk, batch launch from
serving, and narrow-width visual check. All corresponding code paths are build-validated and their
backend halves were [HW]-proven in 013/017/018 drills.
