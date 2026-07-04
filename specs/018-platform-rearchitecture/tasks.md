# Tasks: Platform Re-Architecture — GPU Host Agent, Durable State, Shared Contracts, Closed Loop

**Input**: Design documents from `specs/018-platform-rearchitecture/` (spec.md — clarified
2026-07-02, plan.md, research.md, data-model.md, contracts/{agent-api, platformlib, policy-api,
store-schema}.md, quickstart.md).

> IDs continue the shared space (**T343+**, after 017's T342). Tests are required by the spec
> (FR-177: suite green every phase; SC-113/115) — test tasks are included. **[HW]** = needs the
> target GPU machine (quickstart.md has the drill). Every phase ends at a mergeable checkpoint;
> per-phase rollback is an env flip (quickstart.md §Rollback).

## Format: `[ID] [P?] [Story] Description`

- **[P]** = parallelizable (different files, no dependency). `[USx]` maps to the spec's user stories.

---

## Phase 1: Setup — the shared package both runtimes import

- [x] **T343** Create `platformlib/` (stdlib-only, contracts/platformlib.md): `topology.py`
  (Tenant ids, `ENGINES` registry, `AGENT_PORT=8100`, `STATE_DIR`, legacy `*_URL` resolution),
  `contracts.py` (dataclasses + `validate()`: AgentHealth, EngineState, Admission*, JobSubmit,
  JobRecord, SwapCommand, UnloadResult), `store.py` (object side: promoted S3 helpers with
  module-level client + paginated `list_keys`). Wire imports: gateway `Dockerfile` `COPY
  platformlib/`, host run scripts add repo root to `PYTHONPATH`.
- [x] **T344** Central gateway settings: `gateway/app/settings.py` consuming
  `platformlib.topology`; replace scattered `os.getenv` reads of `TRAINER_URL`/`SERVING_URL`/
  `BENTO_URL`/`EMBED_URL`/`TABULAR_URL`/`ASR_URL`/`SERVING_MODEL`/`MLFLOW_TRACKING_URI` in
  `gateway/app/{swap,serving,evaluation,platform_health,platform_metrics}.py` and
  `gateway/app/routers/{models,runs,batch,monitor,transcribe,vision,embed,tabular}.py`.
- [x] **T345** [P] Config surface: `.env.example` gains `AGENT_URL`, `MLOPS_STATE_DIR`,
  `AGENT_CONTROL_SECRET` (generalizing `SWAP_CONTROL_SECRET`); document per-engine URL flips as
  the migration/rollback mechanism (research R3).

**Checkpoint**: suite green; behavior unchanged (settings is a pure refactor).

---

## Phase 2: User Story 1 — stop dropping/duplicating lifecycle signals (P1) 🎯 MVP

**Goal**: the review's P0 correctness fixes, each independently mergeable (FR-162..167).

**Independent Test**: quickstart.md §US1 — five offline scenarios, no daemon required.

- [x] **T346** [P] [US1] Fail-closed batch guard: `gateway/app/swap.py` `_default_batch_active`
  → trainer unreachable ⇒ treat as active (refuse preempt with "batch state unknown", FR-162);
  extend `tests/test_swap_orchestration.py` + `tests/test_no_preempt_training.py`.
- [x] **T347** [P] [US1] Reserve-before-launch on the PSI path: `gateway/app/routers/monitor.py`
  drift branch calls `quality.try_reserve_retrain` before `_launch_retrain` (release on launch
  failure), unifying with the quality branch (FR-163); extend `tests/test_drift_loop.py` +
  `tests/test_quality_breach.py` with a concurrent double-breach case.
- [x] **T348** [P] [US1] Retained background work: keep strong references to the detached
  logging tasks in `gateway/app/routers/{vision,transcribe}.py` + `gateway/app/stream.py`
  (task-set pattern) and add a `gateway_dropped_work_total` counter beside the existing
  semaphore drops (FR-164); new `tests/test_background_logging.py`.
- [x] **T349** [P] [US1] Complete listings: switch `gateway/app/monitoring.py:latest_reports`
  and `gateway/app/datasets.py:{list_datasets,list_versions}` to
  `platformlib.store.list_keys` (paginated) (FR-165); extend `tests/test_datasets.py`.
- [x] **T350** [US1] Coordination state off `/tmp`: `serving/gpu_lease.py` default path moves to
  `platformlib.topology.STATE_DIR`; every lease participant (both supervisors,
  `serving/bento/service.py`, `training/trainer.py`) verifies at startup it sees the same
  beacon file/inode and fails loud on divergence (FR-166); new `tests/test_state_dir.py`.
- [x] **T351** [P] [US1] Stuck-child parity: port whisper's reap-before-relaunch
  (`serving/whispercpp/supervisor.py:105-110`) into `serving/llama/supervisor.py`'s
  `_ensure_loaded` (FR-167); new regression in `tests/test_serving.py`.

**Checkpoint**: review §5 P0 table fully closed; SC-115 regression tests all exist and pass.

---

## Phase 3: User Story 2 — one agent owns the GPU (P1)

**Goal**: the `hostagent/` consolidation, strangler-style (FR-168..178). One engine per
sub-checkpoint; legacy daemon deleted in the same task that flips its URL.

**Independent Test**: quickstart.md §US2 — offline agent suites per sub-checkpoint; [HW]
SC-106..110 at completion.

### Skeleton

- [x] **T352** [US2] `hostagent/` skeleton: `main.py` (ThreadingHTTPServer on `AGENT_PORT`,
  read surface per contracts/agent-api.md), `metrics.py` (/metrics), `hostagent/run.sh`;
  register daemon `agent` in `supervisor/supervise.py` (opt-in via `SUPERVISE_DAEMONS` during
  migration); add `hostagent` scrape job to `infra/prometheus/prometheus.yml`.
- [x] **T353** [US2] `hostagent/admission.py`: in-process single-slot admission (est-vs-live
  free VRAM, static-budget fallback), `pynvml` with ~1s TTL cache (R1; add to host
  requirements), **lockfile-interop shim** — acquire/release `serving/gpu_lease.py` for agent
  tenants while any legacy tenant remains (FR-166/168).
- [x] **T354** [US2] `hostagent/lifecycle.py`: shared tenant lifecycle (load → ready → drain →
  idle-release → unload; reap resident-but-unready; `wedged` surfaced and admission-blocking;
  `unavailable(reason)` for missing prerequisites, R7) + the adapter interface
  (`spawn/ready/forward/drain/estimate_vram`) per data-model.md (FR-170).
- [x] **T355** [US2] `hostagent/swap.py`: transactional evict→free→load under the admission
  lock; holder kind `job` ⇒ structural refusal (FR-171/172); `POST /control/unload` with
  `X-Agent-Control` (R6).
- [x] **T356** [US2] `hostagent/journal.py`: append-only JSONL in `STATE_DIR`, replay on start,
  `running` → `interrupted(reason)` + alert metric (FR-173, R9).
- [x] **T357** [P] [US2] Agent unit suites: `tests/test_agent_admission.py` (thread-hammer, no
  TOCTOU), `tests/test_agent_lifecycle.py` (fake engine incl. unavailable/wedged/reap),
  `tests/test_agent_swap_txn.py` (contender never wins mid-swap), `tests/test_agent_journal.py`
  (kill −9 replay), `tests/test_lockfile_interop.py` (agent vs legacy mutual exclusion).

### Fold-ins (one mergeable phase each)

- [x] **T358** [US2] LLM fold-in: `hostagent/adapters/llama.py` (from
  `serving/llama/supervisor.py` — spawn llama-server on a dynamic port, ready probe, forward
  incl. SSE `/engines/llm/infer/stream`); flip `SERVING_URL` →
  `http://…:8100/engines/llm`; delete `serving/llama/supervisor.py` (+ its `run.sh` entry);
  gateway `serving.py` reads agent health shape. **[HW]** smoke per quickstart.
  — BUILT (offline: adapter + generic `/engines/{id}/{verb}[/stream]`/health/unload-now surface
  + `adapters` registry; `SERVING_URL`/compose/supervise wiring; supervisor retired; 18 new tests
  + shared-lifecycle drain/reap coverage supersedes the retired supervisor tests; 325 passed /
  24 skipped offline, ruff-clean). **[HW] smoke pending on the GPU box.**
- [ ] **T359** [US2] ASR fold-in: `hostagent/adapters/whisper.py` (multipart forward, opt-in
  engine, `unavailable` when the CUDA build is absent); flip `ASR_URL`; delete
  `serving/whispercpp/supervisor.py` (keep `build.sh`). **[HW]** smoke.
- [ ] **T360** [US2] Vision fold-in: `hostagent/adapters/vision.py` wrapping the BentoML child
  (R10); strip the in-service lease/unload code from `serving/bento/service.py` (admission is
  the agent's job now); flip `BENTO_URL`; gateway `vision.py` drops the `busy`-marker mapping
  in favor of agent 409s. **[HW]** smoke.
- [ ] **T361** [P] [US2] CPU fold-ins: `hostagent/adapters/{embed,tabular}.py` (children, no
  admission); flip `EMBED_URL`/`TABULAR_URL`; retire their supervise entries.
- [x] **T362** [US2] Jobs fold-in: `hostagent/jobs.py` ports the trainer daemon's four launch
  paths into one parameterized submit (subprocess-per-run via `training/run_flow.py` +
  `run_shadow.py` unchanged; child pid = VRAM owner via lifecycle); `POST /jobs` +
  legacy-route aliases per contracts/agent-api.md; flip `TRAINER_URL`; retire
  `training/trainer.py`; journal-backed `GET /jobs`. **BUILT (branch `018/t362-jobs-foldin`):**
  JobManager over admission (`kind="job"`, structurally preempt-proof) + the durable journal;
  jobs hold the single job slot; batch runs off-lease with `gpu_batch_active`; `/health` is a
  superset carrying the trainer fields so swap.py/platform_metrics keep working (byte-compat,
  FR-177); supervise set shrinks to `{agent, ui}`; agent runs under the training venv. 406 passed
  offline. **FR-176 path-injection seam retirement DEFERRED to T362.1** (own PR — it moves the
  gateway evaluation/batch/shadow cores into `platformlib`, a gateway-wide restructure the jobs
  surface doesn't need): `training/scoring/__init__.py:_load_evaluation`,
  `training/flows/hpo.py:_load_evaluation`, `training/flows/batch_infer.py:_load_batch`,
  `training/flows/shadow_replay.py:_load_gateway_shadow` → `platformlib` imports.
- [ ] **T363** [US2] Gateway swap thinning: `gateway/app/swap.py` reduces to preempt-flag
  passthrough (the agent orchestrates); `platform_health.py`/`platform_metrics.py` read the
  agent's single health (parallelize the remaining probes with `asyncio.gather`);
  `tests/test_swap_orchestration.py` re-targeted at the passthrough contract.
- [x] **T364** [US2] Lockfile retirement: delete `serving/gpu_lease.py` + the interop shim +
  `supervisor/supervise.py` shrinks to `{agent, ui}` (unconditional backoff restart, FR-178);
  rewrite `tests/test_gpu_lease.py` → agent admission API; update `tests/test_supervisor.py`
  and the `require_*` reachability fixtures in `tests/conftest.py` to the `{agent, ui}` daemon
  set; single `AGENT_URL` replaces the six daemon URLs in compose + settings; free ports
  8090–8095/8099 removed from `platformlib.topology`.
- [x] **T365** [US2] **[HW]** On-hardware sweep SC-106..110 per quickstart (process count,
  latency parity vs runbook baselines, swap-contention stress incl. `scripts/swap_stress.py`,
  restart-journal drill, gateway-down scrape + zero-fork watch); record results in
  `docs/on-hardware-validation-018.md`.

**Checkpoint**: 2 resident native processes; all five modalities via the agent; suite green.

---

## Phase 4: User Story 3 — the loop closes by declaration (P2, parallelizable after Phase 2)

**Goal**: declarative per-model policies → scheduled checks → modality-correct retrain →
suggest/auto promotion (FR-179..183). Depends on Phase 2 only (T347's reserve-before-launch);
runs against trainer *or* agent jobs surface.

**Independent Test**: quickstart.md §US3 — offline scheduler/promotion suites; [HW] SC-112 drill.

- [x] **T366** [P] [US3] Policy contracts: `ModelPolicy`, `PendingRetrain`,
  `PromotionSuggestion`, `AuditRecord` in `platformlib/contracts.py` with write-time validation
  (known modality with a fine-tune flow, interval ≥60s, ≥1 monitor);
  `tests/test_policy_crud.py` (validation core).
- [x] **T367** [US3] Policy CRUD: `gateway/app/policies.py` + `gateway/app/routers/policies.py`
  (`GET/PUT/DELETE /policies[/{model}]`, `GET /policies/{model}/status`), MinIO-backed pre-US4;
  BFF entries in `ui/lib/gw-allowlist.ts`.
- [x] **T368** [US3] Scheduler: `gateway/app/scheduler.py` lifespan task per
  contracts/policy-api.md — interval ticks through existing `monitoring`/`quality` checks;
  breach → reserve → launch retrain with the policy's `modality` + `latest` dataset resolution;
  busy ⇒ queue-of-one `PendingRetrain` with backoff/supersede (FR-181/182), persisted beside
  the policies (MinIO pre-US4, `platformlib.store`) so a gateway restart resumes the parked
  retrain (R5); tick metrics; `tests/test_policy_scheduler.py` (fake clock + restart-resume
  case).
- [x] **T369** [P] [US3] Modality-aware direct retrain parity: `RetrainSpec` in
  `gateway/app/routers/monitor.py` (+ `monitoring/drift.py` CLI) gains `modality` +
  `dataset_version: "latest"` so the manual path matches policy behavior (closes review §4.1);
  extend `tests/test_drift_loop.py`.
- [x] **T370** [US3] Promotion modes: post-run evaluation (gate + latest shadow verdict) →
  `manual` no-op / `suggest` creates suggestion + accept/dismiss endpoints (accept routes
  through the existing gated `promote()`) / `auto-on-green` promotes + `AuditRecord`; gate
  warn/blocked ⇒ auto falls back to suggest (FR-183); `tests/test_promotion_modes.py`.
- [x] **T371** [US3] UI: policy editor + status strip on `ui/app/monitor/page.tsx`; suggestions
  (gate + shadow verdicts, accept/dismiss) + audit rows on `ui/app/models/page.tsx`; `ui/lib/
  gw.ts` returns structured `{status, body}` errors (retiring the `'-> 409'` string-match in
  `ui/app/runs/page.tsx`); allowlist additions.
- [ ] **T372** [US3] **[HW]** SC-112 loop drill per quickstart (declared vision policy →
  injected breach → suggestion, zero manual steps); append to `docs/on-hardware-validation-018.md`.

**Checkpoint**: Principle IV loop closed; `manual` default verified byte-for-byte.

---

## Phase 5: User Story 4 — durable, queryable state (P3)

**Goal**: high-churn state to the `gateway` database via the shared client (FR-184..186).

**Independent Test**: quickstart.md §US4; [HW] SC-111.

- [ ] **T373** [US4] Relational client: `platformlib/store.py` gains `connect()` +
  idempotent `bootstrap()` DDL per contracts/store-schema.md; `psycopg[binary]` added to
  `gateway/requirements.txt` + host venv lock; schema mirrored in
  `infra/postgres/init.sql`; `tests/test_store_client.py`.
- [ ] **T374** [US4] Cutover — predictions/labels/capture: `gateway/app/quality.py`
  `log_prediction`/`attach_label`/`capture_input` write rows (write-once = unique constraint ⇒
  `LabelExists`, FR-185; fail-open + dropped-counter on store outage); `window()` query
  replaces `_load_pairs` and `shadow.resolve_window`'s join (FR-186);
  `tests/test_label_write_once.py`; retire the in-process `_label_write_lock` and the
  dual-runtime `sys.path` hacks in `gateway/app/{quality,batch,shadow}.py` (FR-176 — shared
  code now lives in `platformlib`).
- [ ] **T375** [US4] Cutover — jobs/policies/suggestions: agent `journal.py` writes `jobs` rows
  directly (clarify Q4; JSONL path retired after import); `policies.py` + suggestion state move
  to their tables; gateway job reads via store.
- [ ] **T376** [P] [US4] Backfill: `scripts/backfill_store.py` (objects → rows,
  `ON CONFLICT DO NOTHING`, counts report, journal import); `tests/test_backfill.py`
  (idempotent re-run).
- [ ] **T377** [US4] **[HW]** SC-111: 10k-prediction window <5s; concurrent-label trial;
  restart drill (gateway + agent) with intact history; append to the runbook doc.

**Checkpoint**: no O(N) object scans on any monitoring path; restarts lose nothing.

---

## Phase 6: Polish & governance

- [ ] **T378** Constitution **v1.5.x wording refresh** (R8) — operator decision, mirrors 017's
  T342: Principle II mechanism sentence names the host agent's in-process admission as the
  lease realization (rule text unchanged). Update `.specify/memory/constitution.md` history
  note on ratification.
- [ ] **T379** [P] Docs refresh: `README.md` status paragraph (stale at "through 014") + the
  architecture mermaid diagram → agent topology; prune retired env vars from compose comments.
- [x] **T380** [P] SC-114 demonstration: stub-engine adapter test in
  `tests/test_agent_adapters.py` proving a new engine = one adapter + one registry row (zero
  edits elsewhere).

---

## Dependencies & execution order

```text
Setup (T343→T345) ──► US1 (T346..T351, all [P] except T350)  🎯 MVP
        │                     │
        │                     ├──► US3 (T366..T372) — needs only T347 + Setup
        │                     ▼
        └──────────► US2 skeleton (T352..T357) ─► fold-ins T358→T359→T360→T361→T362
                                                        │
                                                T363 → T364 → T365 [HW]
US4 (T373..T377) — after US2 T362 (jobs rows) and US3 T367 (policies table); backfill T376
anytime after T373. Polish T378..T380 last (T378 awaits the operator).
```

- **Parallel opportunities**: T346–T349+T351 (five files, five test modules); T357's five
  suites; T361 with T360; the whole US3 track against the US2 fold-in track; T376 with T374/375.
- **MVP scope**: Phase 1 + Phase 2 (Setup + US1) — the platform stops dropping/duplicating
  signals with zero topology change. Each subsequent phase is an independently shippable
  increment per the constitution's Principle VII.
- **[HW] gating**: T358/359/360 smoke, T365, T372, T377 need the RTX 5070 Ti box; everything
  else lands offline with the suite green (house practice: on-hardware SCs recorded in the
  runbook doc).
