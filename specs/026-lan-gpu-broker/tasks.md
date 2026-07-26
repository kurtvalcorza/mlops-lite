# Tasks — 026 LAN Self-Service GPU Broker

**Input**: [spec.md](./spec.md), [plan.md](./plan.md), [data-model.md](./data-model.md), [research.md](./research.md), [contracts/](./contracts/)

Ordered by the revised delivery phasing in [plan.md](./plan.md): **P1 → P3 → P4**, with **P2** and **P5**
present but **gated** (see Phase 5 / Phase 6). Tasks marked `[P]` may run in parallel with their siblings.

**Conventions**
- `[USn]` traces a task to a spec user story; every story carries coverage.
- Foundational tasks carry no story tag — they unblock several stories at once.
- A task is done when its stated check passes, not when the code merely exists.

**Gate discipline.** Phase 5 (jobs) and Phase 6 (sessions) MUST NOT begin before their stated gates
clear. They are listed so story coverage is complete and the sequencing is explicit — not because they
are ready to build.

---

## Phase 0 — Foundational (blocks everything; no story tag)

- [ ] **T618** Create the `tenants`, `api_keys`, `quotas`, `usage_ledger`, and `usage_reservation` tables per [data-model.md](./data-model.md), with Alembic migration and rollback. Check: migration applies and reverts cleanly against a scratch Postgres.
- [ ] **T619** `[P]` Implement `api_key` issuance and hashing (store `key_hash` + non-secret `prefix`; raw key returned once, never persisted). Check: a raw key never appears in the DB or logs.
- [ ] **T620** `[P]` Implement bearer-token auth middleware in the gateway resolving key → tenant, refusing when `api_key.status != 'active'` OR `tenant.status != 'active'` (FR-002). Check: revoking either status refuses the next request.
- [ ] **T621** Add the reserved **system tenant** used by policy-triggered retrains (see T651). Check: it exists after migration and cannot be deleted via the admin API.
- [ ] **T622** Characterization tests capturing today's single-slot `hostagent/admission.py` behaviour before the redesign touches it. Check: tests pass against unmodified `admission.py` and are referenced by T634.
- [ ] **T623** Extend `.specify/memory/hardware-profile.md` tunables into a typed config object (`safety_reserve`, `safety_headroom`, `max_admission_attempts`, `drain_timeout`, `job_drain_timeout`, `admission_backoff`). Check: every tunable referenced by [contracts/admission-scheduler.md](./contracts/admission-scheduler.md) resolves from config, none hardcoded.

---

## Phase 1 — P1: private multi-tenant inference [US1] + metering foundation [US3]

- [ ] **T624** `[US1]` Implement `POST /admin/tenants` and `POST /admin/tenants/{id}/keys` per [contracts/admin-api.md](./contracts/admin-api.md). Check: raw key shown exactly once; rotation invalidates the prior key.
- [ ] **T625** `[US1]` `[P]` Implement the OpenAI-compatible surface (`/v1/chat/completions`, `/v1/models`) per [contracts/inference-openai.md](./contracts/inference-openai.md). Check: an unmodified OpenAI client library completes a request against it.
- [ ] **T626** `[US1]` Enforce **TLS** on all LAN-facing endpoints; reject plaintext. Check: an `http://` request is refused, not redirected, and the quickstart/user-guide examples use `https://` URLs only.
- [ ] **T627** `[US1]` Per-tenant request attribution end to end: every inference request records `tenant_id` on its usage row. Check: two tenants issuing identical requests produce separately attributed records.
- [ ] **T628** `[US3]` Implement the **hard, atomic** `usage_reservation` reserve step — reject on overflow, keyed idempotently by request id (FR-016). Check: concurrent requests against a quota with room for one produce exactly one grant. Note: supersedes R5's original "soft check" wording, corrected in [research.md](./research.md).
- [ ] **T629** `[US3]` Implement **settle-to-actual** on completion, with outbox/WAL retry for settlements that cannot be persisted. Check: killing the process mid-settle still settles exactly once on restart.
- [ ] **T630** `[US3]` Implement recurring-window quota evaluation (window truncation, derived `consumed`/`remaining`, `403 quota_exhausted`). Check: crossing a window boundary restores service with no manual reset.
- [ ] **T631** `[US3]` Reconciliation assertion: ledger totals equal the sum of settled reservations. Check: a property test over randomized reserve/settle/abandon sequences holds the identity.
- [ ] **T632** `[US1]` Single-resident-model serving path (no co-residency yet), preserving current behaviour. Check: existing inference integration tests pass unchanged.
- [ ] **T633** `[US1]` `[P]` Refuse unauthenticated and cross-tenant access on every route. Check: a tenant's key cannot read another tenant's usage or keys (IDOR probe suite).

**P1 exit**: SC-001, SC-002, SC-011 demonstrable; US1 Independent Test passes.

---

## Phase 2 — P3: GPU coordinator redesign (the core of this feature) [US1]

Implements [contracts/admission-scheduler.md](./contracts/admission-scheduler.md) as revised in PR #74
review. Read that contract before starting — the ordering constraints below are load-bearing, not stylistic.

- [ ] **T634** Replace single-slot `admission.py` with the coordinator state machine (`residents` map with `loading|resident|draining|evicting|rolling_back`, `exclusive_job`, `job_barrier`, `reservations` with `materialized`, per-`model_key` `generation`). Check: T622's characterization tests still pass or their intentional deltas are documented.
- [ ] **T635** Implement stage 1 admission **under lock with no I/O**, computing `accounted`, `unmaterialized`, and `effective_free = live_free − unmaterialized`. Check: a static assertion (or lint) proves no lifecycle call is reachable inside the lock.
- [ ] **T636** Implement the **evict-then-recompute** loop: when either bound fails, select victims, mark `draining`, release the lock, complete eviction, then **re-derive both bounds** on the next bounded attempt. Check: a reservation is never recorded while a victim is still resident and accounted (assert on invariant 3).
- [ ] **T637** Implement the **live-free deduction** of unmaterialized reservations. Check: a test with two concurrent admissions, each individually fitting live-free but jointly exceeding it, grants exactly one.
- [ ] **T638** Implement stage 3 commit and the **split rollback** — record `rolling_back` intent under the lock, `unload()` **outside** it, reacquire only to finalize and bump `generation`. Check: a deadlock-detection test that would trip on an unload-inside-lock implementation passes.
- [ ] **T639** Implement `evict()` with a **bounded** drain: `draining` → wait `active_requests == 0` up to `drain_timeout` → `evicting` → unload → verify NVML free rose. On timeout revert the victim to `resident` and return `EvictFailed`. Check: eviction never interrupts an in-flight request, and a stuck victim does not wedge the coordinator.
- [ ] **T640** Implement caller backoff on `EvictFailed`: consume an attempt, exponential jittered backoff capped per config, re-enter stage 1; refuse `gpu_busy` with `Retry-After` when attempts or deadline are exhausted. Check: no admission path waits unbounded on another tenant's request.
- [ ] **T641** Implement request **coalescing** for a `loading` model (`AwaitLoad`) so concurrent requests for the same model never double-load. Check: N simultaneous first-requests for one model produce exactly one load.
- [ ] **T642** Implement `admit_job()` with the **`job_barrier` set before draining**, and assert `residents`/`reservations` empty under the lock before setting `exclusive_job`. Check: a serving admission racing a job start cannot become co-resident with the job.
- [ ] **T643** Assert-test all three coordinator invariants continuously in CI, not just at admission. Check: randomized concurrent admit/evict/job workload holds all three.
- [ ] **T644** `[P]` Reconcile estimates to the **real post-load delta**, rolling back when drift breaks the budget bound. Check: a deliberately under-estimating model triggers rollback rather than budget violation.
- [ ] **T645** Enable **bounded co-residency** on the serving path behind the corrected bounds. Check: two small models serve concurrently; a third that would breach either bound is refused or triggers eviction.

**P3 exit**: SC-006, SC-010 demonstrable; the three invariants hold under concurrency.

---

## Phase 3 — Scheduler, lanes, and single authority [US1] [US3]

- [ ] **T646** Implement `hostagent/scheduler.py` with the two shape-based lanes: `inference_lane` (interleaved, budget-admitted) and `jobs_lane` (strict FIFO). Check: ordering within each lane is arrival-ordered.
- [ ] **T647** Persist `jobs_lane` in Postgres so ordering survives a host-agent restart. Check: restart mid-queue preserves head-of-line order.
- [ ] **T648** Implement **job-drain mode** (bounded inference burst OR head-job wait bound) that stops admitting new inference until the head job acquires the GPU. Check: a continuous inference load does not starve a queued job — the wait bound is observed.
- [ ] **T649** `[US3]` Implement owner override (pin/pause/reorder queued jobs) that never preempts a running job. Check: reordering a queue with a running head leaves the running job untouched.
- [ ] **T650** Verify `gateway/app/scheduler.py` (018 `PolicyScheduler`) is **left functionally unchanged** — it is drift/retrain monitoring, not a GPU-ordering authority. Check: 018's monitoring→retrain feedback tests still pass.
- [ ] **T651** Route **policy-triggered retrains into `jobs_lane`** under the reserved system tenant (T621) instead of calling `/train` directly. Check: a policy-triggered retrain queues FIFO by arrival, never co-runs with a tenant job, and its GPU-seconds are metered to the system tenant.
- [ ] **T652** Confirm `PolicyScheduler`'s existing `Busy`/park-and-retry path is unaffected — a full jobs lane returns the same `409` contract it already handles (FR-182 queue-of-one preserved). Check: 018's parked-retrain tests pass against the new lane.

---

## Phase 4 — P4: additional serving modalities [US4]

- [ ] **T653** `[US4]` Add ASR serving behind the coordinator, co-resident when both bounds allow. Check: an ASR model and an LLM serve concurrently without evicting each other.
- [ ] **T654** `[US4]` `[P]` Add computer-vision serving behind the coordinator. Check: same co-residency behaviour as T653.
- [ ] **T655** `[US4]` Task-typed request surface for non-chat modalities per [contracts/inference-openai.md](./contracts/inference-openai.md). Check: a transcription request routes to the ASR model, not the chat adapter.
- [ ] **T656** `[US4]` Console surfacing of resident set, per-model VRAM accounting, and live-free headroom. Check: the displayed accounted total matches the coordinator's internal sum during a co-resident workload.
- [ ] **T657** `[US4]` Verify the US4 Independent Test end to end, asserting **both** bounds throughout. Check: the accounted set plus outstanding reservations never exceeds usable budget, and every individual load fit live-free minus unmaterialized minus headroom.

---

## Phase 5 — P2: exclusive jobs [US2] — **GATED, DO NOT START**

**Gate (all three required)**: (1) migration to a **native-Linux GPU host** with real `/dev/nvidia*`
nodes; (2) a **passing re-run** of [spikes/sandbox-feasibility.md](./spikes/sandbox-feasibility.md) on
that host; (3) a **new-runtime constitution amendment**. The spike already proved WSL2 cannot host this
— the GPU is paravirtualized (`/dev/dxg`), so gVisor `nvproxy` and Kata VFIO cannot function. P2 MUST
NOT ship on WSL2 under a weaker rootless-namespace posture presented as compliant (FR-026).

- [ ] **T658** `[US2]` Re-run the sandbox feasibility spike on the native-Linux GPU host and record the result. Check: gVisor or Kata starts a GPU-visible container; otherwise the gate stays closed and Phase 5 stops here.
- [ ] **T659** `[US2]` Ratify the new-runtime constitution amendment covering the sandboxed job runtime. Check: constitution version bumped with a sync impact report listing every dependent artifact — including `hardware-profile.md`.
- [ ] **T660** `[US2]` Implement job submission and the durable job record per [contracts/jobs-api.md](./contracts/jobs-api.md). Check: a submitted job survives a gateway restart before it starts.
- [ ] **T661** `[US2]` Execute tenant job code inside the hardened sandbox: non-root, no host mounts, restricted egress. Check: a job attempting a host mount or unrestricted egress fails closed.
- [ ] **T662** `[US2]` Exclusive-lease acquisition through `admit_job()` (T642), never preempted once running. Check: the second of two submitted jobs waits, then starts automatically on the first's completion.
- [ ] **T663** `[US2]` Per-job GPU-seconds reserved and settled through the same reserve→settle path as inference. Check: each job's metered seconds reconcile against wall-clock lease duration within tolerance.
- [ ] **T664** `[US2]` Verify the US2 Independent Test: two tenants' jobs run in order, artifacts produced, **at no point are two GPU tenants resident**, and both jobs are metered.

---

## Phase 6 — P5: interactive sessions [US5] — **GATED, DO NOT START**

**Gate**: the admission class for interactive sessions is undecided — exclusive lease vs sandboxed job
vs a further constitution amendment. Sessions hold the GPU across human think-time, which neither the
serving nor the job model cleanly covers. Decide before building.

- [ ] **T665** `[US5]` Decide and record the session admission class, with its constitution implication. Check: the decision is written into [research.md](./research.md) and, if it needs an amendment, that amendment is ratified before T666.
- [ ] **T666** `[US5]` Implement session start/attach per [contracts/sessions-api.md](./contracts/sessions-api.md) under the chosen admission class. Check: a session acquires the GPU through the coordinator, never bypassing it.
- [ ] **T667** `[US5]` Implement idle-window and TTL enforcement releasing the GPU lease automatically. Check: an idle session past its window releases the lease without operator action.
- [ ] **T668** `[US5]` Meter session GPU-seconds through reserve→settle. Check: a session's metered time reconciles with its held-lease duration.
- [ ] **T669** `[US5]` Verify the US5 Independent Test: an idle session releases automatically and a TTL-exceeding session is ended.

---

## Phase 7 — Cross-cutting

- [ ] **T670** `[P]` Structured logs and metrics for admission outcomes (grant/share/refuse reason/evict/rollback), per tenant. Check: a refused request is attributable to a specific bound.
- [ ] **T671** `[P]` Operator console views for quotas, ledger totals, and queue state. Check: SC-012 demonstrable.
- [ ] **T672** Update [quickstart.md](./quickstart.md) and [user-guide.md](./user-guide.md) to the shipped surface, replacing illustrative endpoints with real ones and TLS URLs throughout.
- [ ] **T673** Refresh `README.md`'s Principle II description to bounded co-residency once P3 ships — the constitution's outstanding follow-up TODO. Check: README no longer describes the system as single-tenant-in-VRAM.

---

## Traceability

| Story | Priority | Tasks |
|---|---|---|
| US1 — private multi-tenant inference | P1 | T624–T627, T632–T633, T634–T645 (coordinator), T646–T649 |
| US2 — submit-and-queue jobs | P2 (gated) | T658–T664 |
| US3 — quotas, ledger, visibility | P3 | T628–T631, T649, T671 |
| US4 — additional modalities | P4 | T653–T657 |
| US5 — interactive sessions | P5 (gated) | T665–T669 |

Requirements coverage: FR-001–FR-026 and SC-001–SC-012 are traced through the phase exits above and
the per-contract traceability sections in [contracts/](./contracts/).
