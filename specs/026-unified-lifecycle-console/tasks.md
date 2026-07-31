---

description: "Task list for feature 026 — Unified ML Lifecycle Console"
---

# Tasks: Unified ML Lifecycle Console

**Input**: Design documents from `specs/026-unified-lifecycle-console/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Numbering**: Continues after 025 — FR-362+, SC-184+, **T618+**.

**Tests**: INCLUDED — repo convention. Every net-new read surface and every join gets an offline
(web-free, fake-backed) test; GPU-touching legs carry an explicit on-hardware validation task
(constitution gate zero).

**Organization**: Grouped by user story. **US1, US2, US3, US10 are the irreducible core**; US8 and
US9 are the pre-agreed cut line if the increment overruns (plan.md Complexity Tracking).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no incomplete-task dependency)
- **[Story]**: US1 shell · US2 runtime · US3 catalog · US4 training · US5 evaluations · US6 inference
  · US7 deployments · US8 datasets · US9 observability/admin · US10 truthful-state
- **[HW]**: requires the target GPU hardware to validate

---

## Phase 1: Setup

- [ ] **T618** Establish the green baseline: run `make lint test ui-check compose-check spec-check`
  and record the pass state (offline suite counts, Ruff clean, UI build) as the regression reference
  for SC-200. Also record the **idle memory baseline** (`docker stats --no-stream` with the stack up
  and the 021 console open) — SC-199 compares against this number and an unrecorded baseline makes
  the criterion unfalsifiable.
- [ ] **T619** Confirm no schema migration is required (research R7 — endpoints are synthesized, not
  persisted). Verify `git diff master -- platformlib/migrations/` stays empty through the increment.
  If one genuinely emerges, create it as a NEW numbered `platformlib/migrations/*.sql` **before**
  dependent tasks, per FR-438.

---

## Phase 2: Foundational (blocking — every story depends on these)

**Purpose**: the truthfulness properties and the type layer must exist *before* any screen, because
retrofitting data-age, degradation, and conflict handling into finished pages is far more expensive
than building them into the fetch layer (plan.md Summary).

- [ ] **T620** Add the response envelope (`data` / `observed` / `degraded` / `conflict`) as a shared
  helper in `gateway/app/console/__init__.py`, per contracts/console-read-api.md. Every console read
  route returns it. **A partially-degraded projection returns 200 with the reachable parts populated
  and unreachable parts `null`** — it must not fail whole (FR-428), and `null` must never be
  serialized as `0` or `[]`.
- [ ] **T621** [P] Add `platform-types.ts` in `ui/lib/` — the normalized types from data-model.md
  (`PlatformHealth`, `PlatformJob`, `PlatformModel`, `StateConflict`, `RuntimeDevice`,
  `EngineProcess`, `AdmissionRecord`, `JournalEntry`, `Endpoint`, `PredictionRecord`, `TraceDetail`,
  `EvaluationResult`, `DriftReport`, `DatasetVersion`, `Artifact`, `AlertRule`, `DashboardEmbed`,
  `AdminInfo`, `NavArea`, `HeaderModel`, `AttentionItem`). This is the Principle V swappability seam:
  no component may import a backend payload shape directly.
- [ ] **T622** [P] Write `ui/lib/use-live.ts` — the shared live-fetch hook carrying **all** of:
  visibility-gated polling (FR-431/SC-196), exponential backoff with `Retry-After` awareness,
  last-known-good retention with data age (FR-430/SC-195), and per-resource cadence from spec §19.
  **Retention must be bounded** — an unbounded history here is the most likely cause of an SC-199
  footprint regression.
- [ ] **T623** [P] Write `ui/lib/charts/` — six hand-rolled SVG primitives (sparkline, time series
  with band, threshold bar, span waterfall, parallel coordinates, matrix heatmap) per research R11.
  **No charting dependency**; `ui/package.json` dependencies must remain exactly `next`, `react`,
  `react-dom` (SC-198).
- [ ] **T624** Build the `ui/app/(console)/` route group and the ten-area shell — sidebar with
  `NavArea` secondary navigation, and the header from data-model §14 (FR-362/367). Enforce the
  naming rule: no navigation item may be named after a backing service (FR-365).
- [ ] **T625** Implement `GET /console/health` (`PlatformHealth` incl. resolved `mode`) and
  `GET /console/capabilities` in `gateway/app/console/` + `routers/console.py`. `mode` resolves from
  **reachability**, never a configured string (research R14). Capabilities (FR-433) is what lets the interface
  **omit** an unsupported control rather than render one that fails — the mechanism behind FR-418.
- [ ] **T626** Implement the 021→026 redirects for every row of data-model §10 in `ui/app/`, and
  delete the 021 stage directories (`serving`, `data`, `training`, `models`, `monitoring`,
  `retraining`) per research R12. **Keep** `ui/app/api/gw/` and the 004/005 security guards — they
  are not 021 artifacts.
- [ ] **T627** Write `tests/test_ui_redirects.py` — one case per data-model §10 row; every retired
  path resolves to a successor area, none returns not-found (FR-364/SC-186).

**Checkpoint**: the shell renders, navigates, degrades honestly, and no retired path 404s.

---

## Phase 3: User Story 1 — Ten-area shell and Overview (Priority: P1) 🎯 core

**Goal**: the landing view answers health / what's running / what needs attention / what next.

**Independent Test**: load against a running platform; ten areas with secondary nav, live summary
cards each showing data age, a unified active-work table joining three sources, a severity-ranked
attention panel, and a normalized activity timeline.

- [ ] **T628** [US1] Implement `GET /console/attention` in `gateway/app/console/` — severity-ranked
  items covering all nine `AttentionItem.kind` values (FR-373). Include
  `integrity: "verification-failed"` artifacts, which are a data-integrity incident and must surface
  here rather than sit quietly in a detail view (data-model §12).
- [ ] **T629** [P] [US1] Implement `GET /console/activity` — the normalized lifecycle timeline that
  preserves the loop as a *visualization* now that it is no longer navigation (FR-363).
- [ ] **T630** [P] [US1] Implement `GET /console/search` — composed resolver across models, runs,
  datasets, jobs, endpoints, predictions by id or name (FR-368).
- [ ] **T631** [US1] Build `ui/app/(console)/overview/` — the eight summary cards (FR-371), the
  unified active-work table (FR-372), the attention panel, and the activity timeline. Every card
  renders its own data age; `activeJobCount` renders `unknown` (not `0`) when its source is
  unreachable.
- [ ] **T632** [P] [US1] Build the header's health indicator + per-service panel (FR-369/370).
  `critical` is reserved for gateway/database loss; **agent loss is `degraded`**, because CPU
  modalities still serve and asserting otherwise overstates the outage (data-model §2).
- [ ] **T633** [US1] Extend `tests/test_ui_smoke.py` — all ten areas render with their secondary
  navigation; Overview is the landing view; summary cards and the active-work join populate from
  fixtures (SC-184/185).

**Checkpoint**: US1 is independently demonstrable and is the true MVP.

---

## Phase 4: User Story 2 — Runtime and GPU console (Priority: P1) 🎯 core

**Goal**: the GPU runtime becomes inspectable — devices, engines, admission decisions with prose
explanations, and the journal.

**Independent Test**: per-device state matches the agent; a refused request is explained in prose
naming the blocking tenant and the shortfall; the journal pages and filters.

**Note**: the largest net-new backend surface in 026 and the largest unknown — sequenced early so it
fails early if it is going to.

- [ ] **T634** [P] [US2] Write `tests/test_runtime_api.py` — offline, web-free, over a **fake NVML
  reader and fake journal**: device snapshot shape incl. `source` provenance, the admission decision
  ring, journal paging and filters. Pin that a `static` source yields `null` fields and **never
  zeros** (FR-381), and that an unreadable GPU returns `200`, not an error.
- [ ] **T635** [US2] Create `hostagent/devices.py` — per-device snapshot (index, name, uuid, compute
  capability, total/free/used VRAM, utilization, temperature, processes) built on the **existing
  1s-TTL cached `GpuReader`** (FR-375, research R2). **MUST NOT fork a subprocess per request** — that is the
  018 regression NVML was introduced to remove. Side-effect free: it may take the admission lock for
  a consistent read, never to claim, extend, or release.
- [ ] **T636** [US2] Add a bounded in-memory decision ring (default 64) to `hostagent/admission.py`,
  written by `acquire()` as it returns, with the server-composed `explanation` templates from
  data-model §6 (FR-377). **This is a decision history, not a queue** (research R1) — no `pending` value, no
  queue-position field. The ring append must not perform IO or extend the critical section.
- [ ] **T637** [US2] Add a paged/filtered read accessor to `hostagent/journal.py` (FR-380) (cursor by
  sequence, hard cap 500, filters by job/engine/event-type/time). Surface `checksum_state` honestly —
  a `torn` tail entry is **shown as torn**, never silently dropped, because a missing final
  transition is exactly what an operator investigating a crash needs (contracts/runtime-api.md).
- [ ] **T638** [US2] Enrich `EngineState` in `platformlib/contracts.py` with **optional fields only**
  (`pid`, `device_index`, `vram_gb`, `model_identity`, `registry_version`, `started_at`,
  `active_requests`) and populate them in `hostagent/lifecycle.py` (FR-376). **`model_identity` must be the
  agent-reported loaded identity (022), never the registry's desired pointer** — sourcing it from the
  pointer would manufacture the exact falsehood FR-427 exists to prevent. Verify the 018/019
  `/health` + `/engines` conformance tests still pass **unchanged**.
- [ ] **T639** [US2] Add agent routes `GET /runtime/devices`, `GET /runtime/admission`,
  `GET /journal` in `hostagent/main.py` per contracts/runtime-api.md (FR-374/375/377/380). All behind `X-Agent-Key` —
  these are not public probes (023 US2). All read-only.
- [ ] **T640** [US2] Create `gateway/app/runtime.py` — the agent proxy holding `X-Agent-Key`, the
  **only** path to `:8100` (FR-432, research R5). On agent loss return `200` with `data: null` and
  `degraded: ["agent"]`; **never an empty list**, which the console would legitimately render as "no
  devices".
- [ ] **T641** [US2] Add gateway routes `GET /runtime/hosts`, `/runtime/hosts/{host}/devices`,
  `/runtime/engines`, `/runtime/admission`, `/runtime/journal`. `hosts` returns a **list** even with
  one host (FR-374) so multi-host (FR-382) needs no later contract change.
- [ ] **T642** [P] [US2] Build `ui/app/(console)/runtime/` — hosts, per-device topology, engine
  processes, admission decisions, journal. **No control that would preempt a running job** (FR-379);
  refusal is presented as designed behaviour. Fallback-derived values are labelled from the `source`
  field, not guessed.
- [ ] **T643** [P] [US2] Render admission explanations in prose from the server-composed
  `explanation` (FR-378) — the interface must not compose its own wording, or it will drift from
  admission's real reasoning.
- [ ] **T644** [US2] Extend the BFF allowlist with the five `runtime/*` entries
  (contracts/allowlist-delta.md, FR-432). **No agent path may appear** — the console never reaches `:8100`.
- [ ] **T645** [HW] [US2] On the RTX 5070 Ti: verify per-device VRAM, resident engine identity, and
  `registry_version` match the agent exactly and `source` reads `nvml` (SC-201). Confirm
  `model_identity` differs from the desired pointer **during an in-flight activation** — the one
  moment the two legitimately diverge, and the only real proof the field is agent-sourced.
- [ ] **T646** [HW] [US2] On hardware: run a real contention event (fine-tune holds the GPU, vision
  classify refused) and confirm the console shows the correct holder throughout and the refusal reads
  *"job … holds the GPU. A running job is never preempted."* (SC-187/202). Then force a `vram`
  refusal and confirm the explanation names the required amount, largest free block, and device.
  Update quickstart.md §3.2 with the recipe actually used.

**Checkpoint**: the runtime console is the increment's differentiator and ships as its own slice.

---

## Phase 5: User Story 3 — Model catalog and compatibility (Priority: P1) 🎯 core

**Goal**: one catalog across five systems, and an honest "can this run here right now" verdict.

**Independent Test**: catalog rows join registry, artifact, deployment, and evaluation facts; a
compatibility panel distinguishes structural from transient ineligibility for all five modalities.

- [ ] **T647** [P] [US3] Write `tests/test_console_joins.py` (catalog half) — the join is correct
  when a side is **missing**: a registry version with no artifact, an artifact with no registry
  entry, a version with no evaluation. Each renders with the missing side marked absent, **never
  dropped from the list** (spec Edge Cases).
- [ ] **T648** [US3] Implement `gateway/app/console/catalog.py` + `GET /console/catalog` — the join
  per data-model §5 (FR-383). `artifactPresent` requires an **actual object-store existence check**; inferring
  presence from a registry URI is how a console shows a download that 404s.
- [ ] **T649** [US3] Implement `GET /console/catalog/{name}/{version}/compatibility` — verdict from
  gateway contracts + live topology (FR-387/388/389). Enforce the three-way distinction: `incompatible`
  (structural — unresolvable adapter base, missing artifact, capability mismatch),
  `not-currently-eligible` (transient — VRAM or a holder), `unknown` (agent unreachable). **Never
  collapse `unknown` into either** — an unreachable agent is not a compatibility fact.
- [ ] **T650** [US3] Build `ui/app/(console)/models/` — catalog list (FR-384), the nine detail tabs
  (FR-386), the compatibility panel, and navigable lineage back to base model and source run
  (FR-390). Tracking vocabulary is preserved verbatim where displayed (FR-366). An unrecognized modality renders as `unknown`, not filtered out (FR-385).
- [ ] **T651** [US3] Extend the allowlist with the three `console/catalog*` entries.

**Checkpoint**: the catalog answers the platform's central model question.

---

## Phase 6: User Story 4 — Training workspace (Priority: P2)

**Goal**: one unit of work with three identifiers becomes one view.

**Independent Test**: a fine-tune shows a normalized state alongside all three native states, an
execution timeline, a resource panel, and streaming logs.

- [ ] **T652** [P] [US4] Write the job-normalization half of `tests/test_console_joins.py` — every
  row of the data-model §3 table maps to exactly one normalized state **and** retains each native
  state (FR-392/SC-190).
- [ ] **T653** [US4] Implement `gateway/app/console/jobs.py` + `GET /console/jobs`,
  `GET /console/jobs/{id}` — the three-way join (FR-391), timeline (FR-393), and resource panel (FR-394).
- [ ] **T654** [P] [US4] Implement `GET /console/runs`, `GET /console/experiments`,
  `GET /console/studies/{id}/trials` — run and experiment **listing is net-new**; only
  `GET /runs/{id}` existed.
- [ ] **T655** [US4] Build `ui/app/(console)/training/` — experiments, runs, studies, jobs. Each job cross-links to its
  gateway record, agent execution, and tracking run in one interaction (SC-189). Logs
  reuse the existing `/runs/{id}/events` SSE; **no new streaming surface** (research R10). An
  interrupted stream is reported, never silently truncated (FR-395). Studies render parallel
  coordinates, history, importance, and trials (FR-396) **without implying a persistent search
  service exists** (FR-397).
- [ ] **T656** [US4] Extend the allowlist with the five training entries.

---

## Phase 7: User Story 10 — Truthful state (Priority: P2) 🎯 core

**Goal**: conflicts are disclosed, degradation is per-domain, and mode is unmistakable.

**Independent Test**: an induced gateway/agent disagreement produces a conflict banner; each service
stopped in turn produces its documented degradation; fixture mode is badged.

**Note**: the fetch-layer half lands in Phase 2; this phase is the detection logic and its proofs.

- [ ] **T657** [US10] Implement `StateConflict` detection in `gateway/app/console/jobs.py` per
  data-model §4. **Only compare observations within the skew threshold** — beyond it emit
  `skewExceeded` and *suppress the conflict claim*, because a stale read disagreeing with a fresh one
  is not evidence of inconsistency and reporting it would train operators to ignore the banner.
- [ ] **T658** [US10] Derive the `Orphaned` state (gateway says running, agent has no process) and
  ensure it **always** carries a `StateConflict`, never stands alone (data-model §3).
- [ ] **T659** [P] [US10] Build the conflict banner component — both source states with observation
  times, last consistent timestamp, and refresh / inspect-journal actions (FR-427). `reconcile` is
  surfaced but inert in 026 (MVP 3 owns it).
- [ ] **T660** [P] [US10] Build the persistent mode badge (`offline` / `live` / `hardware`) from
  `PlatformHealth.mode` (FR-429).
- [ ] **T661** [US10] Extend `tests/test_ui_resilience.py` with the **full seven-service degradation
  matrix** (data-model §11, SC-193). The load-bearing case: with the agent down, runtime reads `unknown` and
  **jobs are NOT reported stopped** (FR-428). An empty `devices: []` here is a **failing** assertion,
  not a pass.
- [ ] **T662** [US10] Add a conflict-detection test to `tests/test_console_joins.py` — an induced
  disagreement produces a disclosure in 100% of cases, never a silently chosen answer (SC-194).

**Checkpoint**: every other surface can now be trusted not to confidently lie.


---

## Phase 8: User Story 5 — Evaluations, gates, drift (Priority: P2)

**Goal**: gate outcomes carry their evidence; drift is reported with its limits.

**Independent Test**: a failed gate shows the rule, threshold, observed value and comparison basis
without leaving the view; drift shows configurable thresholds and stated limitations.

- [ ] **T663** [P] [US5] Write `tests/test_console_read_api.py` (evaluations half) — modality-native
  metrics are **not** coerced into a common metric (FR-399); a version with no logged metric that is
  not serving reads `not-evaluated`, not an error (FR-402).
- [ ] **T664** [US5] Implement `GET /console/evaluations`, `/console/evaluations/{id}`,
  `/console/gates`, `/console/compare` (FR-398) — failing rule, operator, threshold, observed value,
  comparison basis, metric direction, and any override with its reason (FR-400/401/SC-191).
- [ ] **T665** [US5] Implement `GET /console/drift` (FR-404) returning `thresholds` **inline** so the
  interface never hard-codes the 0.10/0.25 convention (FR-405).
- [ ] **T666** [US5] Build `ui/app/(console)/evaluations/` — runs, comparisons, test sets, gates,
  drift. The comparison workspace separates quality / latency / resources / artifacts / datasets /
  policy (FR-403). **The drift surface states the statistic's limitations on the surface itself**
  (FR-406) — it detects distributional change, does not prove degradation, does not establish
  causality, and depends on baseline and binning.
- [ ] **T667** [US5] Extend the allowlist with the five evaluation entries.

---

## Phase 9: User Story 6 — Inference workspace (Priority: P2)

**Goal**: the inference record becomes readable, with payloads sensitive by default.

**Independent Test**: predictions come from the gateway record; payloads are hidden until explicitly
revealed and never appear in a URL; traces render as a generic span waterfall.

- [ ] **T668** [P] [US6] Write the payload-safety tests in `tests/test_ui_security.py` — no payload
  value in any path or query, reveal requires the explicit `POST` call, and no payload content
  reaches browser telemetry (FR-409/SC-192).
- [ ] **T669** [US6] Implement `gateway/app/console/predictions.py` + `GET /console/predictions`,
  `/console/predictions/{id}` — **from the gateway record, not reconstructed from traces**
  (FR-407). Detail (FR-408) returns `PayloadPreview` **without** `preview`, making default-hidden structural:
  a component cannot render a payload it was never sent.
- [ ] **T670** [US6] Implement `POST /console/predictions/{id}/payload` — the explicit reveal (FR-409/SC-192), with
  the identifier in the **body**. It is a `POST` specifically so no payload reference lands in a URL,
  where it would reach logs, history, and referrers (contracts/console-read-api.md).
- [ ] **T671** [P] [US6] Implement `GET /console/captures` and `GET /console/review-queue` —
  label state per FR-410, prioritized by policy result, confidence, drift contribution, sampling, missing label, manual flag,
  and suggestion (FR-411).
- [ ] **T672** [US6] Implement `gateway/app/console/traces.py` + `GET /console/traces`,
  `/console/traces/{id}` via the already-pinned `mlflow-skinny` client (research R6 — **no new
  dependency**). Normalize to a generic span tree server-side (FR-412) so FR-413 is enforced in one place.
- [ ] **T673** [US6] Build `ui/app/(console)/inference/` — predictions, traces, captures, labels,
  review queue. Trace waterfall uses the T623 primitive and **makes no token-oriented assumptions**
  for non-text-generation modalities (FR-413). Large payloads truncate with the true size stated,
  never loading the whole object (spec Edge Cases).
- [ ] **T674** [US6] Extend the allowlist with the seven inference entries, including the one
  write-shaped `POST console/predictions/:id/payload` (which mutates nothing).

---

## Phase 10: User Story 7 — Deployments read-side (Priority: P2)

**Goal**: desired and resident are never conflated.

**Independent Test**: an in-progress activation shows desired and resident separately and is not
labelled healthy on desired state alone.

- [ ] **T675** [US7] Implement `gateway/app/console/endpoints.py` + `GET /console/endpoints`,
  `/console/endpoints/{id}` (FR-414) — **synthesized from registry, serving pointer, activation state, and
  agent engines; no table, no migration** (research R7).
- [ ] **T676** [US7] Enforce the status rule (FR-415/416/417, data-model §7): `healthy` requires **resident**
  confirmation; desired-only is `pending`; a GPU modality not resident because another tenant holds
  the GPU is **`stopped`, not `failed`** — on-demand loading is the design, and calling it a failure
  misrepresents Principle II as a fault.
- [ ] **T677** [US7] Build `ui/app/(console)/deployments/` — endpoint list and detail. **Render no
  rollout control the gateway does not implement** (FR-418); availability comes from
  `GET /console/capabilities`, so an unsupported control is absent rather than decorative.
- [ ] **T678** [US7] Extend the allowlist with the two endpoint entries.

---

## Phase 11: User Story 8 — Datasets and artifacts (Priority: P3) ✂️ cut candidate

**Goal**: content-addressed data and artifacts with honest integrity, no credential in the browser.

**Independent Test**: a dataset shows digest, validation, and referencing runs/models; the four
integrity states are distinguishable; a path outside permitted prefixes is refused server-side.

- [ ] **T679** [US8] Implement `GET /console/datasets`, `/console/datasets/{name}/{version}`,
  `GET /console/artifacts` per data-model §12 (FR-419). **Integrity is opt-in** (`?verify=true`) — rehashing
  multi-gigabyte objects per render is not viable on this hardware.
- [ ] **T680** [US8] Preserve the four-state integrity distinction (FR-420). `not-verified` ("we did
  not check") must **not** collapse into `verification-unavailable` ("no checksum was ever
  recorded") — materially different facts when deciding whether to trust an artifact.
- [ ] **T681** [US8] Build `ui/app/(console)/datasets/` over the **existing** proxied download route.
  No presigned URL is minted and no object-store credential reaches the browser (FR-421); paths are
  validated against permitted prefixes server-side **before** any upstream call (FR-422).
- [ ] **T682** [US8] Extend the allowlist with the three dataset/artifact entries and add the
  path-rejection case plus the no-credential-in-browser sweep to
  `tests/test_ui_security.py` (SC-197).

---

## Phase 12: User Story 9 — Observability and Administration (Priority: P3) ✂️ cut candidate

**Goal**: curated native panels, honest alert state, and platform administration.

**Independent Test**: native panels render; a firing rule claims no notification; a blocked embed
falls back to an external link; the migration ledger is visible and read-only.

- [ ] **T683** [US9] Implement `GET /console/metrics/summary` and `/console/metrics/series` (FR-423).
  **Bound range and step server-side** so a console panel cannot issue an unbounded query.
- [ ] **T684** [US9] Implement `GET /console/alerts` and `GET /console/dashboards`. `AlertRule`
  carries **no delivery/notification/recipient/acknowledgement field and none may be added** — there
  is no Alertmanager, and such a field would invite the console to imply someone was told (FR-424).
  `embeddable` resolves server-side; `externalUrl` is always populated (FR-425).
- [ ] **T685** [US9] Implement the four `GET /console/admin/*` routes (FR-426). **Never return
  credential material**: report only whether a key is configured and whether the gateway is
  fail-closed. The migration view reads the existing checksummed ledger (023 US4) and **never
  triggers an apply**.
- [ ] **T686** [US9] Build `ui/app/(console)/observability/` and `administration/`. The embed is
  explicitly labelled external, exposes no administrative controls, and degrades to a link (FR-425).
- [ ] **T687** [US9] Extend the allowlist with the eight observability/admin entries; add the
  no-delivery-claim assertion from quickstart §2.9 to the offline suite.

---

## Phase 13: Polish and cross-cutting

- [ ] **T688** Update `ui/lib/gw-allowlist.ts` comment sectioning from the 021 loop vocabulary to the
  ten areas (re-sectioning only — moves no entry, grants no access) and verify the final delta
  matches contracts/allowlist-delta.md exactly.
- [ ] **T689** Export the updated OpenAPI to
  `specs/001-mlops-platform/contracts/openapi.json` and confirm **every** new route appears
  (FR-438/SC-203).
- [ ] **T690** [P] Update `README.md` — the increment-history table (026 row), the console section
  (replace the 021 loop-native description), and the architecture diagram's UI node.
- [ ] **T691** [P] Verify SC-198 mechanically: `ui/package.json` dependencies are still exactly
  `next`, `react`, `react-dom`; the production build succeeds and adds no runtime package. Confirm no broker, scheduler,
  analytics store, or serving runtime was introduced (FR-434).
- [ ] **T692** Measure and record the idle footprint against the T618 baseline (SC-199). A regression
  points at `use-live.ts` retention bounds first, not rendering cost.
- [ ] **T693** Run the full gate — `make lint test ui-check compose-check spec-check` — and confirm
  **no regression** against the T618 baseline (SC-200).
- [ ] **T694** [HW] Run the quickstart Layer 3 recipes end to end on the GPU box and record the
  results in the increment runbook, including the compatibility three-way verdict check across all
  five modalities (SC-188) and the fallback-labelling check (FR-381).

---

## Phase 14: Deferred-scope guards (US11 / US12)

**Purpose**: US11 (MVP 2) and US12 (MVP 3) are specified in 026 but **built** in 027/028. Deferral is
not the absence of work — 026 owes each one a guarantee that the deferral holds and that no
half-built affordance misleads an operator.

- [ ] **T695** [US11] Assert the **read-only guarantee** for every route 026 adds: no
  `gateway/app/console/*` or `runtime/*` route mutates state, and the sole write-shaped entry
  (`POST /console/predictions/{id}/payload`) only reveals an existing payload. Add the assertion to
  `tests/test_console_read_api.py` so a future MVP 2 write path cannot land here by accident instead
  of through the sanctioned gated route (FR-435/436). Record the outcome in [deferred.md](./deferred.md) §MVP 2 — **not** in a new
  `specs/027-*/` directory, which would fail the required `specs` gate: `check_specs.py` demands the
  full six-artifact set per feature directory, so a spec-only stub breaks CI.
- [ ] **T696** [US12] Assert that MVP 3 affordances surfaced in 026 are **inert and labelled as
  such**: the conflict banner's `reconcile` action (T659) performs no reconciliation, and no
  suggestion is auto-applied or auto-accepted anywhere in the console (FR-437). An affordance that
  looks actionable but does nothing is worse than its absence — it teaches operators the console
  lies. Record the outcome in [deferred.md](./deferred.md) §MVP 3 — same reason: no partial
  `specs/028-*/` directory.

---

## Dependencies

```text
Phase 1 (T618-T619)
   └─> Phase 2 Foundational (T620-T627)   ← BLOCKS EVERYTHING
          ├─> Phase 3  US1  (T628-T633)   P1 core
          ├─> Phase 4  US2  (T634-T646)   P1 core  ← largest unknown, sequenced early
          ├─> Phase 5  US3  (T647-T651)   P1 core   [needs US2's device data for compatibility]
          ├─> Phase 6  US4  (T652-T656)   P2       [creates console/jobs.py]
          ├─> Phase 7  US10 (T657-T662)   P2 core   [needs US2 + US4, which now precede it]
          ├─> Phase 8  US5  (T663-T667)   P2
          ├─> Phase 9  US6  (T668-T674)   P2
          ├─> Phase 10 US7  (T675-T678)   P2
          ├─> Phase 11 US8  (T679-T682)   P3  ✂️
          └─> Phase 12 US9  (T683-T687)   P3  ✂️
                 └─> Phase 13 Polish (T688-T694)
                        └─> Phase 14 Deferred-scope guards (T695-T696)
```

**Cross-story dependencies** (the only ones that are real):

- **US3 → US2**: the compatibility panel needs `largestFreeVramGb` from the device snapshot. US3 can
  be built against a fake device source and wired to the real one when T635 lands.
- **US10 → US2, US4**: conflict detection compares agent execution against gateway job state, so it
  needs both join surfaces. Its *fetch-layer* half is already in Phase 2 and blocks nothing.

Every other story is independent and can proceed in parallel once Phase 2 is complete.

## Parallel execution examples

**Phase 2** — T621, T622, T623 are three disjoint new files and can run together.

**Phase 4** — T634 (tests, fake-backed) runs alongside T635/T636/T637, which touch three separate
agent modules. T642 and T643 are both UI and can pair once T641 lands.

**Across stories after Phase 2** — US4, US5, US6, US7 touch disjoint `gateway/app/console/` modules
and disjoint `ui/app/(console)/` directories; their four backend tasks and four UI tasks can run as
two parallel tracks.

## Implementation strategy

**MVP** = Phase 1 + Phase 2 + **US1** (T618–T633). At that point the console replaces 021 with a
working ten-area shell, an honest Overview, and no broken paths — a shippable increment on its own.

**Core** = MVP + **US2 + US3 + US10**. This is the irreducible set from plan.md Complexity Tracking:
the runtime console that justifies the increment, the catalog that answers its central question, and
the truthfulness layer without which every other surface can confidently display a falsehood.

**Cut line**: if the increment overruns, **US8 (Phase 11) and US9 (Phase 12) leave 026** and phase to
027 ahead of MVP 2. Both deepen surfaces that partly exist today, so cutting them degrades rather
than breaks the console. Their allowlist entries and routes come out with them.

**Hardware tail**: T645, T646, and T694 cannot run in a container or from hosted CI. They ship as
explicit `[HW]` tasks in the 023/025 tradition — flagged, never silently skipped — and the offline
suite pins everything around them with injected fakes so only genuinely hardware-dependent behaviour
waits on the RTX 5070 Ti.
