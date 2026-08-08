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

- [X] **T618** Create the `tenants`, `api_keys`, `quotas`, `usage_ledger`, and `usage_reservation` tables per [data-model.md](./data-model.md), with Alembic migration and rollback. Check: migration applies and reverts cleanly against a scratch Postgres.
- [X] **T619** `[P]` Implement `api_key` issuance and hashing (store `key_hash` + non-secret `prefix`; raw key returned once, never persisted). Check: a raw key never appears in the DB or logs.
- [X] **T620** `[P]` Implement bearer-token auth middleware in the gateway resolving key → tenant, refusing when `api_key.status != 'active'` OR `tenant.status != 'active'` (FR-002). Check: revoking either status refuses the next request.
- [X] **T621** Add the reserved **system tenant** used by policy-triggered retrains (see T651). Check: it exists after migration and cannot be deleted via the admin API.
- [X] **T622** Characterization tests capturing today's single-slot `hostagent/admission.py` behaviour before the redesign touches it. Check: tests pass against unmodified `admission.py` and are referenced by T634.
- [X] **T623** Extend `.specify/memory/hardware-profile.md` tunables into a typed config object (`safety_reserve`, `safety_headroom`, `max_admission_attempts`, `drain_timeout`, `job_drain_timeout`, `admission_backoff`). Check: every tunable referenced by [contracts/admission-scheduler.md](./contracts/admission-scheduler.md) resolves from config, none hardcoded.

---

## Phase 1 — P1: private multi-tenant inference [US1] + metering foundation [US3]

- [X] **T624** `[US1]` Implement `POST /admin/tenants` and `POST /admin/tenants/{id}/keys` per [contracts/admin-api.md](./contracts/admin-api.md). Check: raw key shown exactly once; rotation invalidates the prior key.
- [X] **T625** `[US1]` `[P]` Implement the OpenAI-compatible surface (`/v1/chat/completions`, `/v1/models`) per [contracts/inference-openai.md](./contracts/inference-openai.md). Check: an unmodified OpenAI client library completes a request against it.
- [X] **T626** `[US1]` Enforce **TLS** on all LAN-facing endpoints; reject plaintext. Check: an `http://` request is refused, not redirected, and the quickstart/user-guide examples use `https://` URLs only.
- [X] **T627** `[US1]` Per-tenant request attribution end to end: every inference request records `tenant_id` on its usage row. Check: two tenants issuing identical requests produce separately attributed records.
- [X] **T628** `[US3]` Implement the **hard, atomic** `usage_reservation` reserve step — reject on overflow, keyed idempotently by request id (FR-016). Check: concurrent requests against a quota with room for one produce exactly one grant. Note: supersedes R5's original "soft check" wording, corrected in [research.md](./research.md).
- [X] **T629** `[US3]` Implement **settle-to-actual** on completion, with outbox/WAL retry for settlements that cannot be persisted. Check: killing the process mid-settle still settles exactly once on restart.
- [X] **T630** `[US3]` Implement recurring-window quota evaluation (window truncation, derived `consumed`/`remaining`, `403 quota_exhausted`). Check: crossing a window boundary restores service with no manual reset.
- [X] **T631** `[US3]` Reconciliation assertion: ledger totals equal the sum of settled reservations. Check: a property test over randomized reserve/settle/abandon sequences holds the identity.
- [X] **T632** `[US1]` Single-resident-model serving path (no co-residency yet), preserving current behaviour. Check: existing inference integration tests pass unchanged.
- [X] **T633** `[US1]` `[P]` Refuse unauthenticated and cross-tenant access on every route. Check: a tenant's key cannot read another tenant's usage or keys (IDOR probe suite).

**P1 exit**: SC-001, SC-002, SC-003 demonstrable; US1 Independent Test passes.

> SC-011 (hardened job sandbox) is **not** a P1 exit criterion. P1 contains no job implementation, and
> SC-011 is the arbitrary-job isolation outcome the spike proved infeasible on WSL2 — it is gated on the
> P2 migration to a native-Linux GPU host. Requiring it here made P1 unable to satisfy its own exit
> despite being fully deliverable. **SC-011 moves to the P2 exit**, alongside Drill 2b.

---

## Phase 2 — P3: GPU coordinator redesign (the core of this feature) [US1]

Implements [contracts/admission-scheduler.md](./contracts/admission-scheduler.md) as revised in PR #74
review. Read that contract before starting — the ordering constraints below are load-bearing, not stylistic.

- [X] **T634** Replace single-slot `admission.py` with the coordinator state machine (`residents` map with `loading|resident|draining|evicting|rolling_back`, `exclusive_job`, `job_barrier`, `reservations` with `materialized`, per-`model_key` `generation`). Check: T622's characterization tests still pass or their intentional deltas are documented.
- [X] **T635** Implement stage 1 admission **under lock with no I/O**, computing `accounted`, `unmaterialized`, and `effective_free = live_free − unmaterialized`. Check: a static assertion (or lint) proves no lifecycle call is reachable inside the lock.
- [X] **T636** Implement the **evict-then-recompute** loop: when either bound fails, select victims, mark `draining`, release the lock, complete eviction, then **re-derive both bounds** on the next bounded attempt. `select_victims` MUST consider only residents in state `resident` — never one already `draining`/`evicting`/`rolling_back` for another operation. Check: a reservation is never recorded while a victim is still resident and accounted (invariant 3); and two sequential admissions cannot both select the same victim and race two `unload()`s against it.
- [X] **T637** Implement the **live-free deduction** of unmaterialized reservations. Check: a test with two concurrent admissions, each individually fitting live-free but jointly exceeding it, grants exactly one.
- [X] **T638** Implement stage 3 commit and the **split rollback** — record `rolling_back` intent under the lock, `unload()` **outside** it, reacquire only to finalize and bump `generation`. The drift check MUST retain **all other outstanding reservations** (`Σ accounted + real_bytes + Σ other reservations`), not just this load's real bytes, or it can commit a load that breaks invariant 1. Check: a deadlock-detection test that would trip on an unload-inside-lock implementation passes; and a commit is refused when this load fits alone but breaches the budget together with a concurrent reservation.
      Three further stage-3 corrections are tracked as **T674–T676** (Phase 8) — they belong to this
      phase in execution order but carry later IDs to keep task IDs strictly increasing.
- [X] **T639** Implement `evict()` with a **bounded** drain: `draining` → wait `active_requests == 0` up to `drain_timeout` → `evicting` → unload → verify NVML free rose. On timeout return `EvictFailed`, reverting the victim to `resident` **only when no `job_barrier`/`exclusive_job` is set** — under a barrier the victim stays `draining` and its ownership transfers to the job's drain, since the job's one-shot marking pass has already run and would never re-mark a victim that reverted behind it. Check: eviction never interrupts an in-flight request; a stuck victim does not wedge the coordinator; and a job submitted while an *unrelated* eviction is stalled still acquires the GPU rather than timing out behind a victim that silently reverted to `resident`.
- [X] **T640** Implement caller backoff on `EvictFailed`: consume an attempt, exponential jittered backoff capped per config, re-enter stage 1; refuse `gpu_busy` with `Retry-After` when attempts or deadline are exhausted. Check: no admission path waits unbounded on another tenant's request.
- [X] **T641** Implement request **coalescing** for a `loading` model (`AwaitLoad`) so concurrent requests for the same model never double-load. Check: N simultaneous first-requests for one model produce exactly one load. The waiter registry and disposition rules this depends on are **T682–T683**.
- [X] **T642** Implement `admit_job()` with the **`job_barrier` set before draining**, and assert `residents`/`reservations` empty under the lock before setting `exclusive_job`. It MUST first return `Wait(retry_after)` when an `exclusive_job` or another `job_barrier` owner already exists — never overwrite a live claim. The drain MUST mark **every** resident `draining`, not only the idle ones — the barrier already refuses new admissions, so all counts strictly decrease and the one-shot pass converges; marking only idle residents makes the drain unable to finish whenever a model is serving. On timeout, revert every surviving victim to `resident`. Check: a serving admission racing a job start cannot become co-resident with the job; of two jobs admitted against an already-empty serving set, exactly one runs while the other waits; and a job submitted while a model is actively serving still acquires the GPU once those requests finish, rather than timing out. **No longer blocked** — both former OPEN decisions are closed in [contracts/admission-scheduler.md](./contracts/admission-scheduler.md) §Jobs; the commit-side half is T690.
- [X] **T643** Assert-test all **five** coordinator invariants continuously in CI, not just at admission. Check: randomized concurrent admit/evict/job workload holds all five, including the claim-count invariant (4) and the waiter-disposition invariant (5).
- [X] **T644** `[P]` Reconcile estimates to the **real** measured size via a **per-PID** NVML reading (`used_by_pid(child.pid)`) taken under `load_gate`, not a device-wide free-memory delta — with concurrent loads a device-wide delta is unattributable and can double-account one model while under-accounting another. Roll back when drift breaks the budget bound. Check: a deliberately under-estimating model triggers rollback rather than budget violation; and two models loading concurrently are each accounted their own size.
- [X] **T645** Enable **bounded co-residency** on the serving path behind the corrected bounds. Check: two small models serve concurrently; a third that would breach either bound is refused or triggers eviction.

**P3 exit**: SC-006, SC-010 demonstrable; the five invariants hold under concurrency.

---

## Phase 3 — Scheduler, lanes, and single authority [US1] [US3]

- [X] **T646** Implement `hostagent/scheduler.py` with the two shape-based lanes: `inference_lane` (interleaved, budget-admitted) and `jobs_lane` (strict FIFO). Check: ordering within each lane is arrival-ordered.
- [X] **T647** Persist `jobs_lane` in Postgres so ordering survives a host-agent restart. Check: restart mid-queue preserves head-of-line order.
- [X] **T648** Implement **job-drain mode** (bounded inference burst OR head-job wait bound) that stops admitting new inference until the head job acquires the GPU. Check: a continuous inference load does not starve a queued job — the wait bound is observed.
- [X] **T649** `[US3]` Implement owner override (pin/pause/reorder queued jobs) that never preempts a running job. Check: reordering a queue with a running head leaves the running job untouched.
- [X] **T650** Verify `gateway/app/scheduler.py` (018 `PolicyScheduler`) is **left functionally unchanged** — it is drift/retrain monitoring, not a GPU-ordering authority. Check: 018's monitoring→retrain feedback tests still pass.
- [ ] **T651** Route **policy-triggered retrains into `jobs_lane`** under the reserved system tenant (T621) instead of calling `/train` directly. Check: a policy-triggered retrain queues FIFO by arrival, never co-runs with a tenant job, and its GPU-seconds are metered to the system tenant.
  > **Reopened.** This was marked complete on the strength of `test_a_policy_retrain_enters_the_jobs_lane_under_the_system_tenant`, which enqueues through `store.enqueue_broker_job` directly and never involves `PolicyScheduler`. The lane semantics it pins are real; the **routing the task actually names is not implemented**. `gateway/app/scheduler.py::_default_launch` still `POST`s `/train`, and no production caller exists for `enqueue_broker_job`, `note_job_queued`, or `admit_head_job` — the persisted `queued → running → terminal` machine has no driver outside tests. The single-authority gap this task exists to close is therefore still open, and `hostagent/scheduler.py`'s own docstring still describes it in the present tense.
  >
  > **Not blocked by the Phase 5 gate.** That gate covers sandboxing *tenant* code (FR-026); a system-tenant retrain runs platform code and needs no sandbox. What it needs is a jobs-lane runner for system-tenant work — take the persisted head, `admit_head_job()`, transition to running, execute the existing `/train`, settle, `end_job()`, and hydrate head/wait state from the recovered queue on restart. That is a strict subset of T660–T664 with the isolation requirement removed, and it can be built on WSL2.
- [X] **T652** Confirm `PolicyScheduler`'s existing `Busy`/park-and-retry path is unaffected — a full jobs lane returns the same `409` contract it already handles (FR-182 queue-of-one preserved). Check: 018's parked-retrain tests pass against the new lane.

---

## Phase 4 — P4: additional serving modalities [US4]

- [X] **T653** `[US4]` Add ASR serving behind the coordinator, co-resident when both bounds allow. Check: an ASR model and an LLM serve concurrently without evicting each other.
- [X] **T654** `[US4]` `[P]` Add computer-vision serving behind the coordinator. Check: same co-residency behaviour as T653.
- [X] **T655** `[US4]` Task-typed request surface for non-chat modalities per [contracts/inference-openai.md](./contracts/inference-openai.md). Check: a transcription request routes to the ASR model, not the chat adapter.
- [X] **T656** `[US4]` Console surfacing of resident set, per-model VRAM accounting, and live-free headroom. Check: the displayed accounted total matches the coordinator's internal sum during a co-resident workload.
- [X] **T657** `[US4]` Verify the US4 Independent Test end to end, asserting **both** bounds throughout. Check: the accounted set plus outstanding reservations never exceeds usable budget, and every individual load fit live-free minus unmaterialized minus headroom.

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

**P2 exit**: SC-011 demonstrable via Drill 2b on the native-Linux host (moved here from the P1 exit,
where it was unsatisfiable — P1 ships no job code), plus US2's Independent Test. This exit is reachable
only once all three gate conditions above are met.

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

- [X] **T670** `[P]` Structured logs and metrics for admission outcomes (grant/share/refuse reason/evict/rollback), per tenant. Check: a refused request is attributable to a specific bound.
- [X] **T671** `[P]` Operator console views for quotas, ledger totals, and queue state. Check: SC-012 demonstrable.
- [X] **T672** Update [quickstart.md](./quickstart.md) and [user-guide.md](./user-guide.md) to the shipped surface, replacing illustrative endpoints with real ones and TLS URLs throughout.
- [X] **T673** Refresh `README.md`'s Principle II description to bounded co-residency once P3 ships — the constitution's outstanding follow-up TODO. Check: README no longer describes the system as single-tenant-in-VRAM.

---

## Phase 8 — Round-3 review corrections

Findings from the PR #74 re-review that have no home in an existing task. **Execution order follows the
phase noted on each**, not the ID — IDs are late only because task IDs must be strictly increasing.

- [X] **T674** `[US1]` *(Phase 2)* Implement the **load-failure** path: on spawn error, readiness timeout, OOM, or missing model, drop the reservation and the `loading` entry under the lock (guarded by `generation`), unload any partial child **outside** the lock, and fail every `AwaitLoad` waiter with the same outcome. Check: a model whose load always fails does not permanently reserve capacity, and a later request for it attempts a fresh load rather than awaiting a load that no longer exists.
- [X] **T675** `[US1]` *(Phase 2)* Implement the **transient-state branch** (`AwaitTransient`): a request whose `model_key` is `draining`, `evicting`, or `rolling_back` awaits the owning operation instead of falling through to a fresh load. Check: a request arriving mid-rollback does not create a competing `loading` entry that the rollback finalizer then deletes.
- [X] **T676** `[US1]` *(Phase 2)* Distinguish **transient contention from a genuinely oversized model**: `413 model_too_large` only when the estimate exceeds `usable_capacity − safety_headroom`; otherwise retryable `gpu_busy` with `Retry-After`. Check: a model that fits alone but is blocked by another tenant's outstanding reservation gets a retryable answer, not a permanent 413.
- [X] **T677** `[US1]` *(Phase 2)* Implement the **request-claim lifecycle**: `Share` increments under the observing critical section; `Grant` and every `AwaitLoad` joiner increment in stage 3's commit section atomically with `state = resident`; release is `finally`-style on every terminal path (success, error, disconnect, deadline), exactly once, updating `last_used_at`. Check: a freshly loaded model is never observed idle while its triggering request runs (an eviction racing a cold `Grant` cannot unload it); and a randomized request workload leaves `active_requests == 0` with no model left permanently un-evictable.
- [X] **T678** `[US3]` *(Phase 1)* Bind every `usage_reservation` to its **`window_start`** at reserve time, and charge reserve/settle/release against that stored window rather than the window in force at completion. Check: a job reserved just before a window boundary and completing after it is charged to the *old* window, and cannot be double-spent against the new one's budget.
- [ ] **T679** `[US2]` *(Phase 5, gated)* Release or settle the reservation on **job cancellation**, atomically with the state change: `queued → cancelled` releases in full; `running → cancelled` settles elapsed GPU-seconds and releases the remainder. Idempotent under repeated cancel. Check: cancelling jobs in a loop does not progressively exhaust a tenant's quota.
- [X] **T680** `[US2]` *(Phase 3)* Implement **restart recovery** for the persisted jobs lane: `queued` jobs stay `queued` in `queue_pos` order (they must not be swept to `interrupted` by `hostagent/journal.py`'s existing startup rewrite), the formerly-`running` job resolves to a terminal state, and its reservation is settled-to-elapsed with the remainder released. Check: a restart mid-queue preserves FIFO order and leaves no reservation stranded in `reserved`.
- [X] **T681** `[US1]` *(Phase 1)* Emit settled usage for **streaming** completions as a terminal `usage` SSE event before `[DONE]`, and do **not** send `X-GPU-Seconds` on streamed responses (headers precede the body, so no settled value exists yet). Check: a streamed completion reports final GPU-seconds without buffering the body; a non-streamed one still carries the header.

---

## Phase 9 — Round-4/5 review corrections and closed design decisions

Findings from the PR #74 round-4 re-review, the round-5 self-review, and the two design decisions the
owner closed in round 5. As with Phase 8, **execution order follows the phase noted on each task**, not
the ID: T682–T684 are prerequisites of T641, and T690 is the commit-side half of T642 — neither can ship
without the other, since the barrier is only airtight when both the drain and the commit honour it.

- [X] **T682** `[US1]` *(Phase 2)* Add the **waiter registry**: `reservation.waiters`, with registration (stage 1, `loading` branch), deregistration (waiter deadline), and claim assignment (stage 3 commit) all performed **under the state lock**. Check: the commit's count-of-joiners and assignment-of-claims occur in one critical section, so a waiter cannot deregister between them; and no code path enumerates waiters outside the lock.
- [X] **T683** `[US1]` *(Phase 2)* Implement **`dispose(waiters, outcome)`** on **every** load-owning exit — commit, spawn-failure, *and the commit-time rollback* (drift/stale), which round 3 left abandoning its joiners. Waiters adopt the loader's disposition; retryable outcomes (`Retry`, `gpu_busy`) make the waiter consume an attempt and re-enter stage 1, terminal ones (`load_failed`, `model_too_large`) are returned verbatim. Check: with a load forced to roll back on drift, every joined waiter is woken with a retryable outcome inside the loader's own timeframe — none waits out its deadline; and a forced spawn failure returns `load_failed` to all joiners without any of them re-attempting the same failing load.
- [X] **T684** `[US1]` *(Phase 2)* Implement the `AwaitLoad` **deadline** exit with its claim tie-break: a waiter that times out deregisters under the lock, **unless** the commit already assigned it a claim, in which case it takes the claim and releases it normally. Check: a stress test that expires waiter deadlines concurrently with commits leaves `active_requests == 0` afterwards, with no model left permanently un-evictable (invariants 4 and 5).
- [X] **T685** `[US2]` *(Phase 3)* Carry **`interrupted`** as a distinct terminal `job.state` and report it verbatim from `/jobs/{id}` rather than folding it into `failed`. Check: a host-agent restart mid-job leaves the job `interrupted` with elapsed GPU-seconds settled, and a tenant can distinguish it from a job their own code failed; the pre-broker `hostagent/jobs.py` surface still maps `interrupted → failed`.
- [ ] **T686** `[US5]` *(Phase 6, gated)* Split session timers: idle-cull keys on **`last_gpu_activity_at`** (admitted GPU work only); `POST /heartbeat` updates `last_heartbeat_at` and **must not** reset the GPU idle timer. Check: a session heartbeating on its normal client interval with no cells running still releases its GPU lease within `idle_timeout_s` (SC-007).
- [X] **T687** `[US1]` *(Phase 2)* Enforce **reservation ownership**: an operation drops its own reservation on **every** exit including the stale path; `evict()` and any other reclaimer remove only the resident entry and bump the generation, never another op's reservation. Check: a load that finds its generation bumped still releases its reserved bytes — `accounted` and `unmaterialized` return to their pre-admission values, and a subsequent admission for the same size succeeds.
- [X] **T688** `[US1]` *(Phase 1)* Return **one status per refusal code**: `gpu_busy` → `503` with `Retry-After` on every inference route (exclusive job, contention, exhausted attempts alike); `model_too_large` → `413` only when the estimate exceeds `usable_capacity − safety_headroom`. The host agent's jobs-lane-full `409` (FR-182) stays distinct. Check: a client retrying on 503 eventually succeeds for every transient cause, and no contention case ever surfaces as 413.
- [X] **T689** `[US3]` *(Phase 3)* Expose both terms of **both VRAM bounds** on `GET /admin/queue` — `usable_capacity`, `accounted`, `reserved`, `live_free`, `safety_headroom`, plus per-resident `state`/`active_requests`, outstanding `reservations`, and `job_barrier` — named as [contracts/admission-scheduler.md](./contracts/admission-scheduler.md) names them. Check: Drill 3 step 3 can assert invariants 1 and 2 by reading this response alone, with no recourse to agent logs.
- [X] **T690** `[US1]` *(Phase 2)* Add the **barrier re-check to stage 3's commit** — `barred = job_barrier or exclusive_job` as a third rollback condition alongside `stale`/`drift`, unloading outside the lock and disposing joiners with retryable `gpu_busy`. Check: a load in flight when a job's barrier rises never becomes resident — `admit_job`'s closing `assert residents empty and reservations empty` cannot be tripped by it — and the refused request retries successfully once the job completes.

## Implementation status (as of this branch)

**58 of 73 tasks complete.** Phase 0 (foundational), Phase 1 (P1 inference + metering), Phase 2 (the
coordinator redesign), Phase 4 (modalities), Phase 7 (cross-cutting), and all of Phases 8–9's review
corrections are done and verified. Phase 3 is complete **except T651**.

**15 open tasks — 14 gated, 1 not.** The gated ones are unstarted on purpose per the gate discipline
at the top of this file; T651 is open because it was closed prematurely:

| Open tasks | Phase | Gate |
|---|---|---|
| **T651** | 3 (single authority) | **none — this one is buildable now.** Reopened: its test exercised the lane, not the routing, and `PolicyScheduler` still calls `/train` directly. Needs a system-tenant jobs-lane runner. See the task for why the Phase 5 sandbox gate does not apply to it. |
| T658–T664, T679 | 5 (P2 exclusive jobs) | native-Linux GPU host + a **passing** sandbox re-run + a new-runtime constitution amendment. The spike is complete and negative: WSL2's GPU is paravirtualized (`/dev/dxg`, no `/dev/nvidia*`), so gVisor `nvproxy` and Kata VFIO cannot function. |
| T665–T669, T686 | 6 (P5 sessions) | the session admission class is undecided (T665) — exclusive lease vs sandboxed job vs a further amendment. |

Neither *gate* can be cleared from a container: one needs different hardware, the other needs an owner
decision and a constitution amendment. Starting either would mean shipping the weaker posture FR-026
exists to prevent, or building against an admission class nobody has chosen. T651 is subject to
neither and is simply outstanding work.

**A note on what "complete" has to mean here.** T651 passed review because a test with the task's name
on it was green. It enqueued through the store by hand, so it could never have failed for the reason
the task cared about. Where a task names a *production path*, the check has to run that path — a test
that reproduces the mechanism beside it proves the mechanism, not the wiring.

**Co-residency ships behind `BROKER_COORDINATOR_ADMISSION=1`, default off.** The coordinator, the
lanes, and the whole broker surface are complete and tested; the flag is the phase gate made
operational, since P3/P4 are verified on hardware before they become the default. With it off the
agent's behaviour is byte-identical to 018's.

---

## Traceability

| Story | Priority | Tasks |
|---|---|---|
| US1 — private multi-tenant inference | P1 | T624–T627, T632–T633, T634–T645 (coordinator), T646–T649, T674–T677, T681, T682–T684, T687–T688, T690 |
| US2 — submit-and-queue jobs | P2 (gated) | T658–T664, T679–T680, T685 |
| US3 — quotas, ledger, visibility | P3 | T628–T631, T649, T671, T678, T689 |
| US4 — additional modalities | P4 | T653–T657 |
| US5 — interactive sessions | P5 (gated) | T665–T669, T686 |

Requirements coverage: FR-001–FR-026 and SC-001–SC-012 are traced through the phase exits above and
the per-contract traceability sections in [contracts/](./contracts/).
