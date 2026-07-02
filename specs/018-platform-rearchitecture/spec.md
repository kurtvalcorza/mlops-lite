# Feature Specification: Platform Re-Architecture — GPU Host Agent, Durable State, Shared Contracts, Closed Loop

**Feature Branch**: `018-platform-rearchitecture`

**Created**: 2026-07-02

**Status**: Draft

**Input**: The target architecture from the 2026-07 architecture review
([docs/architecture-review-2026-07.md](../../docs/architecture-review-2026-07.md), reviewed on
PR #26). Today the single-GPU invariant (Principle II) is enforced by a **cross-process lockfile
protocol** reimplemented by four separate native daemons (llama supervisor, whisper supervisor,
BentoML vision, trainer), babysat by a fifth process (`supervisor/supervise.py`), with preemptive
swap brokered from a sixth (the gateway). The review found this shape working but fraying: the
duplicated supervisor code has **already diverged in correctness** (whisper reaps a stuck child
before relaunch; llama does not), tenant names and ports are hand-allocated string literals
scattered across both runtimes (an on-hardware port collision already shipped), health polling
forks `nvidia-smi` continuously, the swap handoff is not transactional (the freed GPU can be
sniped between evict and load), trainer job history lives in process memory, high-churn quality
state is O(N) sequential object-store reads, and the Principle IV feedback loop is half-open
(manual checks, LLM-only retrain, inert shadow verdicts). 018 is the consolidation increment:
**one GPU host agent** owning admission in-process with engines as supervised children, **one
shared contracts package** across gateway and host, **relational storage** for high-churn state
on the already-resident database, and **declarative per-model policies** that close the loop.

> **Review-derived decisions (2026-07-02):**
> 1. **One native `gpu-host-agent` process owns all GPU admission.** Admission becomes an
>    in-process, race-free-by-construction decision (live VRAM reads without per-call process
>    forks). The cross-process lockfile protocol — flock sidecar, PID start-time liveness,
>    `vram_pid` tracking, acquire-time self-heal — exists *only because* tenants are separate
>    processes; with one owner it reduces to a lock plus child management. The agent stays
>    **torch-free** (like today's trainer daemon): every engine runs as a **child process**
>    behind one shared tenant lifecycle (load-on-demand → serve → drain → idle-release/unload)
>    with thin per-engine adapters (llama.cpp, whisper.cpp, torch vision, CPU embed, CPU
>    tabular). The trainer's subprocess-per-run CUDA isolation is preserved unchanged.
> 2. **Swap becomes transactional.** Evict → free → load executes under a single admission
>    decision inside the agent; no other tenant can acquire between. Training/HPO/batch remain
>    never-preemptable — now enforced structurally (the agent *knows* job state; the current
>    fail-open trainer-unreachable guard becomes impossible by construction).
> 3. **Lease-as-API during migration, lockfile retired at the end.** Engines fold into the agent
>    one at a time (strangler style); until the last external tenant folds in, the agent
>    participates in the existing lockfile protocol so both worlds interoperate. The lockfile is
>    deleted only in the final phase, together with the tests that assert its internals (rewritten
>    against the agent's admission API in the same phase).
> 4. **High-churn relational state moves to the already-resident database.** Prediction logs,
>    ground-truth labels, the capture index, and job records move into the provisioned-but-unused
>    `gateway` database (zero new resident services — Principle III). MinIO remains the blob
>    store: datasets, model artifacts, captured payloads, reports.
> 5. **One shared `platform` contracts package** — tenant identities, port/topology registry,
>    typed health/job payload schemas, storage client — installed by both the gateway image and
>    the native host. Deletes the seven-file env-var sprawl, the duplicated holder→URL maps, and
>    the dual-runtime `sys.path` hacks.
> 6. **The loop becomes declarative.** Each monitored model gets a policy (monitors, check
>    interval, on-breach retrain of the *breached* modality on the *current* dataset, promotion
>    mode) reconciled by a scheduler in the always-on gateway. Promotion modes: `manual`
>    (default, today's behavior), `suggest` (one-click operator confirm), `auto-on-green`
>    (gate pass + optional shadow-win; every automatic action audited). Shadow-replay verdicts
>    stop being inert: `suggest`/`auto` consume them.
> 7. **Groundwork lands first.** The review's P0 correctness fixes ship as the first phase,
>    independent of consolidation: fail-closed batch guard, PSI cooldown reserve-before-launch,
>    retained references for detached logging tasks, GPU coordination state off `/tmp`,
>    object-listing pagination, and llama/whisper stuck-child parity.

> **Scope note**: 018 changes the platform's *shape*, not its *surface*. The gateway's external
> API (routes, status-code semantics 400/409/502/503/507), the UI/BFF, the MLflow registry flow
> (tags, aliases, gated promote), the content-addressed dataset registry, score-at-registration,
> and all five modalities' behavior are preserved. Requirement IDs continue the shared space
> (FR-162+, SC-106+, tasks T343+). Migration is phase-gated (Principle VII): the existing test
> suite is green at every merged phase.

> **Hard boundary (NON-NEGOTIABLE)**: **Principle II — at most one GPU tenant resident at any
> instant — is preserved at every instant of every phase**, including mid-migration (the agent
> honors the lockfile protocol while any external tenant remains) and mid-swap (evict → free →
> load stays strictly sequential). **Training/HPO/batch are never preempted.** Idle infra stays
> ≤ ~3 GB RAM; **no new resident service** is introduced (the agent *replaces* five native
> daemons plus the babysitter; the database is already resident for MLflow). The frozen GPU
> stack (torch/CUDA versions) is untouched.

> **Builds on**: 008/017 GPU lease + swap (semantics preserved, mechanism consolidated) · 013/016
> prediction/label/capture pipeline (storage relocated) · 011/015 gate + score-at-registration
> (consumed by promotion policies) · the 2026-07 architecture review (findings §4.1–§4.7, target
> architecture §5–§6).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — The platform stops dropping or duplicating lifecycle signals (Priority: P1)

As the operator, I need the correctness fixes the review rated P0 — independent of any
re-architecture — so that preemption can never evict a serving model that an active GPU batch is
driving, concurrent drift checks can never double-launch retrains, background prediction/trace
logging is never silently dropped, listings never silently truncate, and the GPU mutex can never
be voided by two daemons seeing different coordination state.

**Why this priority**: These are live correctness holes in shipped behavior (008–017). They are
small, independently landable, and several become structural non-issues after consolidation — but
the platform should not run exposed while the larger work proceeds.

**Independent Test**: Unit tests with injected seams (the existing harness pattern): trainer
unreachable ⇒ preempt refused; two concurrent breach checks ⇒ exactly one retrain; a burst of
fire-and-forget logs ⇒ all complete or are counted dropped; >1000 stored reports ⇒ complete
listing; coordination-state divergence ⇒ loud startup failure.

**Acceptance Scenarios**:

1. **Given** a GPU batch is driving a serving engine and the trainer daemon is unreachable,
   **When** a request arrives with `preempt=true`, **Then** the preempt is **refused** with an
   explicit "batch state unknown" reason (fail-closed), and the batch completes undisturbed.
2. **Given** drift and quality checks run concurrently and both detect a breach, **When** both
   attempt to launch a retrain, **Then** exactly one retrain launches and the other observes the
   reserved cooldown.
3. **Given** a stream of served predictions with background logging, **When** the process is
   under load, **Then** every log either completes or increments a visible dropped-counter —
   none vanish silently.
4. **Given** more than 1,000 drift reports (or dataset versions), **When** the operator lists
   them, **Then** the listing is complete.

---

### User Story 2 — One agent owns the GPU (Priority: P1)

As the operator, I bring the platform up and a **single native host agent** supervises every
model engine as a child process: it admits at most one GPU tenant in-process, loads engines on
demand, drains and idle-releases them, executes training/HPO/batch/shadow jobs via the existing
subprocess isolation, performs transactional swaps, journals job records durably, and exposes its
own health/metrics endpoint that the monitoring stack scrapes directly. Serving behavior — every
modality, every status code, cold-load versus warm latency — is indistinguishable from 017.

**Why this priority**: This is the headline consolidation. It eliminates the duplicated,
already-diverged supervisor protocol code (the platform's most likely source of the next
co-residency bug), makes swap transactional, ends the port sprawl and the `nvidia-smi` fork
storm, removes the observability single-point-of-failure, and shrinks resident native processes
from ~8 to 2 (agent + UI) — a direct Principle III win.

**Independent Test**: Full-stack bring-up on the target machine: exactly two resident native
processes; all five modalities serve; concurrent cross-modality stress (LLM infer + vision
classify + ASR transcribe + preempt mixes) shows zero co-residency in VRAM sampling and zero
lease losses; agent restart preserves job history; Prometheus shows GPU metrics with the gateway
stopped.

**Acceptance Scenarios**:

1. **Given** a cold platform, **When** the operator runs bring-up, **Then** exactly the agent
   and the UI are resident natively, and every modality answers its smoke request.
2. **Given** the LLM is resident and idle past its timeout, **When** the idle reaper runs,
   **Then** VRAM returns to baseline and the next request cold-loads — same semantics as 017.
3. **Given** the LLM is resident, **When** a vision request arrives with `preempt=true` and the
   operator has confirmed, **Then** the swap executes evict → free → load as one atomic
   admission decision, and **no other tenant can acquire the GPU between those steps** (verified
   under concurrent contention).
4. **Given** a training run is active, **When** any preempt-flagged request arrives, **Then** it
   is refused (409) — structurally, not via a network probe that can fail open.
5. **Given** jobs have run (train/HPO/batch/shadow), **When** the agent restarts, **Then** job
   history and terminal states survive; any job interrupted by the restart is marked failed with
   a reason rather than vanishing.
6. **Given** the gateway container is stopped, **When** Prometheus scrapes, **Then** GPU
   free-VRAM, holder, and per-engine health metrics are still collected (direct scrape of the
   agent).
7. **Given** an engine child wedges during load (resident-but-unready), **When** the next
   request arrives, **Then** the agent reaps the stuck child and relaunches — uniformly for
   every engine (parity with whisper's current fix, which llama lacks).
8. **Given** continuous UI status polling for one minute, **When** GPU state is read, **Then**
   zero per-poll subprocess forks occur (live VRAM reads come from an in-process interface with
   bounded-staleness caching).

---

### User Story 3 — The feedback loop closes by declaration (Priority: P2)

As the operator, I declare a policy per monitored model — which monitors to run (input drift,
quality-vs-ground-truth), how often to check, what to do on breach (retrain the **breached
modality** on the **current** dataset version), and the promotion mode (`manual` / `suggest` /
`auto-on-green`) — and the platform runs the loop without me: scheduled checks, breach → correct
retrain, score-at-registration, gate verdict, and a visible promotion suggestion (or audited
auto-promotion) that consumes shadow-replay verdicts. Drift detected while I sleep becomes a
scored, gated, promotion-ready candidate by morning.

**Why this priority**: Principle IV promises a *closed* loop; the review showed it is half-open —
checks are manually triggered, a breach can only launch an **LLM** retrain regardless of the
breached modality, retrains train on a pinned dataset version, and shadow verdicts are consumed
by nothing. This story makes the constitution's core promise true. It depends only on User
Story 1's trigger fixes — it can land before, after, or in parallel with the agent consolidation.

**Independent Test**: With a policy declared for a vision model and an injected quality breach:
within one check interval a **vision** retrain launches on the latest dataset version, registers,
scores, gates, and surfaces a promotion suggestion — with zero manual invocations between
detection and suggestion. Busy-GPU and double-breach variants covered by unit tests.

**Acceptance Scenarios**:

1. **Given** a policy with a 15-minute interval, **When** no human touches the platform for a
   day, **Then** checks have run on schedule and their reports/metrics are visible.
2. **Given** a quality breach on the ASR model, **When** the policy fires, **Then** the launched
   retrain is an **ASR** fine-tune on the **latest** registered dataset version — not an LLM run
   on a pinned one.
3. **Given** the GPU is busy with training when a breach fires, **When** the retrain cannot
   launch, **Then** exactly one pending retrain is parked and retried with backoff — the breach
   is not dropped, and training is not preempted.
4. **Given** promotion mode `suggest` and a retrained version that passes the gate (and wins its
   shadow window, when one exists), **When** the run completes, **Then** the operator sees a
   one-click promotion suggestion citing the gate verdict and shadow verdict.
5. **Given** promotion mode `auto-on-green`, **When** the candidate passes the gate, **Then** it
   is promoted automatically and the action is durably audited (who/what/why/when); `manual`
   mode continues to behave exactly as today.

---

### User Story 4 — Platform state is durable and queryable (Priority: P3)

As the operator, quality windows, shadow-replay windows, label attachment, and job histories are
served from the platform's already-resident relational database instead of thousands of
sequential object-store round trips and process memory — so monitoring stays fast as prediction
volume grows, label write-once is a real constraint rather than an in-process lock, and nothing
about the platform's recent history evaporates on a restart.

**Why this priority**: Valuable and load-bearing for scale, but demo-scale workloads function
without it; the loop (User Story 3) works at current volumes on the existing store. It lands last
so migration risk never blocks the correctness and consolidation wins.

**Independent Test**: Seed ≥10,000 predictions with labels; a quality window computes in seconds
(today: minutes of sequential reads); duplicate label submissions are rejected by the store under
concurrency; listings paginate correctly; blobs (payloads, reports, artifacts) remain on object
storage and reports remain reproducible after the cutover.

**Acceptance Scenarios**:

1. **Given** ≥10,000 logged predictions, **When** a quality or shadow window resolves, **Then**
   it completes within seconds via store-side filtering/joins rather than per-object reads.
2. **Given** two concurrent label submissions for the same prediction id, **When** both commit,
   **Then** exactly one wins — enforced by the store, correct even with multiple writer
   processes.
3. **Given** the platform restarts (agent and gateway), **When** the operator lists runs,
   studies, batches, and shadow jobs, **Then** history from before the restart is intact.

---

### Edge Cases

- **Agent crash mid-swap**: engine children share the agent's process group and die with it —
  VRAM frees; on restart the journal shows the interrupted swap/job as failed with reason. At no
  point were two tenants resident.
- **Agent crash mid-training**: today a trainer-daemon crash already loses the run's tracking (in-
  memory state); under 018 the journal marks the run failed and the operator is told — strictly
  better, though the child still dies with the agent. Restarting the agent is an operator action;
  its supervisor backoff must not flap it while a long run is active.
- **GPU telemetry unavailable** (driver hiccup): admission falls back to the static budget
  headroom check, as today — never fail-open into co-residency.
- **Wedged child in uninterruptible sleep**: kill fails; the agent keeps the tenant marked
  resident (never lies about VRAM), surfaces a distinct "wedged" state on health/metrics, and
  refuses new GPU admissions — the operator finally *sees* the condition that today silently pins
  the GPU.
- **Migration half-state**: while llama is folded in but whisper is not, the agent holds the
  lockfile on behalf of its tenants and whisper contends as before — one-tenant invariant intact
  across the boundary; verified by a cross-boundary contention test in each such phase.
- **Database unavailable**: prediction/label logging degrades fail-open with a visible dropped
  counter (as object-store logging does today); control-plane operations that need the store
  (windows, job history) fail loud with 502-class errors; serving is unaffected.
- **Policy misconfiguration** (unknown modality, zero interval, missing dataset): rejected at
  declaration time with a structured error — never discovered at breach time.
- **Two breaches, one GPU** (drift and quality, different models): the shared cooldown plus the
  single parked-retrain slot serialize them; second breach parks or observes cooldown — no storm.
- **Port collision on child launch**: child ports are allocated dynamically by the agent —
  the 8081/8082 fixed-port EADDRINUSE loop and the 8099-style collisions cease to exist.

## Requirements *(mandatory)*

### Functional Requirements

**Groundwork (User Story 1)**

- **FR-162**: The preemption batch guard MUST fail **closed**: when the state of GPU batch work
  cannot be determined, preempt-flagged requests are refused with an explicit reason.
- **FR-163**: All retrain triggers (input-drift and quality) MUST reserve the shared cooldown
  **before** launching; concurrent checks across both signals yield at most one launch.
- **FR-164**: Fire-and-forget logging work (predictions, traces, captures) MUST either complete
  or be counted in a visible dropped-work metric; it MUST NOT be silently discarded.
- **FR-165**: Every stored-object listing surfaced to the operator (drift reports, datasets,
  versions) MUST return complete results regardless of storage page limits.
- **FR-166**: GPU coordination state MUST NOT live in a per-boot, per-namespace location; every
  GPU participant MUST verify at startup that it observes the same coordination state as its
  peers, failing loud on divergence. (Transitional: superseded when the lockfile retires.)
- **FR-167**: Stuck-child recovery (reap a resident-but-unready engine before relaunch) MUST
  behave identically for every engine. (Transitional llama/whisper parity; structural under
  FR-170.)

**GPU host agent (User Story 2)**

- **FR-168**: A single native host-agent process MUST be the sole admission authority for GPU
  residency; the admission decision (live free-VRAM check against the tenant's estimate, or
  static-budget fallback) MUST be race-free by construction, with no
  time-of-check/time-of-use window.
- **FR-169**: The agent MUST remain free of GPU/ML runtime dependencies; every engine executes
  as a supervised **child process**. Training/HPO/batch/shadow keep the existing
  subprocess-per-run isolation and result protocol.
- **FR-170**: All engines MUST share one tenant lifecycle — load-on-demand, readiness probe,
  drain, idle-release, unload, stuck-child reap — with per-engine specifics confined to an
  adapter; adding an engine touches its adapter and a registry entry, never lifecycle, admission,
  or swap logic.
- **FR-171**: Preemptive swap MUST be transactional: evict → free → load executes under one
  admission decision such that no third tenant can acquire the GPU between eviction and target
  load. Operator confirmation and per-request opt-in semantics (017) are unchanged.
- **FR-172**: A running training/HPO/batch job MUST never be preempted, enforced from the
  agent's own job state — no network probe, no fail-open path.
- **FR-173**: Job records (runs, studies, batches, shadow replays) MUST be journaled durably as
  they change state; an agent restart preserves history and marks interrupted jobs failed with a
  reason.
- **FR-174**: The agent MUST expose health and metrics (GPU free VRAM, holder, per-engine state,
  job states) scraped **directly** by the monitoring stack; loss of the gateway MUST NOT blind
  GPU/host observability.
- **FR-175**: Live GPU telemetry MUST be read in-process with bounded-staleness caching; steady-
  state health/status polling MUST NOT fork a subprocess per call.
- **FR-176**: A single shared contracts package MUST define — exactly once, for both the gateway
  and the native host — tenant identities, the port/endpoint topology, typed schemas for
  health/job/admission payloads, and the storage client. Cross-runtime imports via path
  manipulation MUST be eliminated; child ports are allocated dynamically by the agent.
- **FR-177**: The gateway's external API contract (routes, request/response shapes, status-code
  semantics including 409-busy and 507-too-large) and all UI behavior MUST be preserved
  throughout; each migration phase merges with the full existing test suite green. Tests that
  assert retired internals (the lockfile protocol) are rewritten against the agent's admission
  API in the same phase that retires them.
- **FR-178**: On completion, resident native processes MUST be reduced to the agent and the UI;
  the standalone process babysitter either retires or shrinks to supervising exactly those two;
  the lockfile protocol and its `/tmp` state are deleted.

**Declarative policies (User Story 3)**

- **FR-179**: The operator MUST be able to declare, per monitored model: monitors to run, check
  interval, on-breach action, and promotion mode (`manual` default / `suggest` /
  `auto-on-green`); declarations are validated at write time and visible in the UI.
- **FR-180**: A scheduler in the always-on control plane MUST execute policy checks on their
  declared intervals with no external trigger; every check is observable (metrics + report).
- **FR-181**: An on-breach retrain MUST target the **breached model's modality** and resolve the
  dataset **dynamically** (latest registered version by default); the launched run flows through
  the existing register → score-at-registration → gate pipeline unchanged.
- **FR-182**: When a breach cannot launch (GPU busy), exactly one pending retrain MUST be parked
  per model and retried with backoff until launched or superseded; breaches never preempt
  training and are never silently dropped.
- **FR-183**: In `suggest` mode, a candidate that passes the gate (and wins its shadow window
  when one exists) MUST surface a one-click promotion suggestion citing both verdicts; in
  `auto-on-green` mode the promotion executes automatically with a durable audit record;
  `manual` preserves today's behavior exactly. Shadow-replay verdicts MUST be consumed by
  `suggest`/`auto` evaluation and surfaced alongside gate verdicts.

**Durable state (User Story 4)**

- **FR-184**: Prediction logs, ground-truth labels, the capture index, and job records MUST move
  to the platform's already-resident relational database (the provisioned `gateway` database);
  blobs (datasets, artifacts, captured payloads, reports) remain on object storage. No new
  resident service.
- **FR-185**: Label attachment MUST be write-once enforced by a store constraint — correct under
  concurrent writers across processes.
- **FR-186**: Quality and shadow windows MUST resolve via store-side filtering and joins with
  latency bounded in the thousands-of-predictions regime (SC-111); a one-time backfill migrates
  existing prediction/label objects, and prior reports remain readable.

### Key Entities

- **Host Agent**: the single native process owning GPU admission, engine children, job
  execution, and host observability. Replaces four daemon supervisors plus the babysitter.
- **Engine Adapter**: per-engine launch/readiness/request specifics (llama.cpp, whisper.cpp,
  torch vision, CPU embed, CPU tabular) behind the shared tenant lifecycle; CPU engines are
  exempt from admission, identical in lifecycle.
- **Tenant**: an admitted GPU resident — a serving engine or a job's subprocess; at most one
  exists at any instant.
- **Job Record**: durable state of a run/study/batch/shadow replay — id, kind, modality,
  request, state transitions, result/error; survives restarts.
- **Model Policy**: per-model declaration — monitors, interval, breach action, promotion mode;
  validated at write time.
- **Promotion Suggestion**: a gate-passing (and shadow-winning, when applicable) candidate
  awaiting operator confirmation in `suggest` mode; carries both verdicts. Auto-promotions
  produce an **Audit Record** instead.
- **Prediction / Label / Capture-Index Records**: relocated high-churn rows keyed by prediction
  id — model version, modality, timestamps, label (write-once), payload pointer into object
  storage.
- **Contracts Package**: the single shared definition of tenant identities, topology, typed
  payload schemas, and storage access used by both runtimes.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-106**: Full platform bring-up on the target machine yields **exactly 2 resident native
  processes** (agent + UI), down from ~8, with all five modalities passing their smoke requests.
- **SC-107**: Idle infrastructure stays ≤ ~3 GB RAM, and cold-load and warm-inference latency
  for every modality are within 10% of the 017 baseline recorded in the on-hardware runbook.
- **SC-108**: A concurrent contention stress (parallel LLM/vision/ASR requests with preempt
  mixes, ≥100 swap cycles) observes **zero** instants with two GPU tenants resident and **zero**
  occurrences of a third tenant acquiring between evict and load.
- **SC-109**: After an agent restart with prior jobs recorded, 100% of terminal job states are
  still listable, any interrupted job reports failed-with-reason within 10 seconds of restart,
  and VRAM returns to baseline.
- **SC-110**: With the gateway stopped, GPU free-VRAM/holder/engine metrics continue arriving in
  the monitoring stack (direct scrape), and one minute of continuous UI status polling causes
  **zero** per-poll subprocess forks on the host.
- **SC-111**: A quality or shadow window over ≥10,000 logged predictions resolves in under 5
  seconds (from minutes today); concurrent duplicate label submissions produce exactly one
  stored label in 100% of trials.
- **SC-112**: With a declared policy and an injected breach, a correct-modality retrain on the
  latest dataset launches within one check interval, and a promotion suggestion (or audited
  auto-promotion) appears with **zero manual invocations** between detection and suggestion.
- **SC-113**: Every migration phase merges with the full existing test suite green (218 tests at
  review time) and the UI smoke/security/resilience tests passing unmodified; the default
  (non-preempt, manual-promotion) request paths behave byte-for-byte as in 017.
- **SC-114**: Adding a hypothetical new serving engine requires one new adapter module plus one
  registry entry — zero edits to admission, lifecycle, swap, or gateway routing code
  (demonstrated by a stub-engine test).
- **SC-115**: The correctness regressions class-fixed by this increment is empty at completion:
  no fail-open preemption path, no double-fire trigger, no silently-dropped background log, no
  truncated listing, no divergent per-engine recovery behavior — each verified by a dedicated
  regression test.

## Assumptions

- **No constitution amendment is expected.** Principle II mandates "a single, race-free
  GPU lease: a single-slot admission mechanism" — it does not mandate the lockfile
  implementation; in-process admission inside one owner *strengthens* the guarantee. The plan's
  Constitution Check MUST confirm this reading (as 017 did for its wording change), and the
  hybrid-GPU workflow amendment (native host processes for GPU work) already covers the agent.
- **The already-resident database is not a "new resident service"** under Principle III; the
  `gateway` database has been provisioned since 001 and is currently unused. If this increment
  is descoped, that database should be deleted instead (the review's alternative).
- **BentoML embed/tabular/vision services fold in as child processes first** (least churn — the
  agent adopts their run scripts); replacing BentoML with plain adapters is optional later work,
  not required by this spec.
- **The UI keeps its own process** (Node runtime, per the v1.3.0 amendment); the surviving
  babysitter (or the agent) restarts it on crash. Restart backoff must respect active training
  (edge case above).
- **Policy storage** rides the durable-state layer when present (User Story 4) and a versioned
  file/config surface before that — the declaration UX is identical either way.
- **Existing S3 prediction/label objects are backfilled once** into the relational store at
  cutover; object-store reports remain readable in place; no dual-write period is required at
  single-operator scale.
- **Scheduler placement**: the loop scheduler runs in the always-on gateway container (it needs
  no GPU and must outlive host restarts); the agent executes, the gateway decides — mirroring
  today's trainer split.
- **Migration order** (phase-gated, each independently shippable): groundwork (US1) → contracts
  package → agent skeleton + first engine (LLM) → remaining engines fold in one per phase →
  trainer/job execution folds in → lockfile retires → policies (US3, parallelizable after US1)
  → durable state (US4). The plan may reorder within these constraints.

## Non-Goals

- **No cluster, no multi-node, no multi-GPU, no multi-replica gateway** — the single-box,
  single-operator assumptions remain load-bearing simplifications (Principles I–III).
- **No message broker, no Redis, no always-on Prefect/Optuna server, no workflow engine** — the
  scheduler is a lightweight in-process loop; jobs remain subprocess-per-run.
- **No serving-engine replacement** — llama.cpp, whisper.cpp, torch vision, sentence-transformers
  embed, and LightGBM tabular stay; the frozen GPU stack is untouched.
- **No registry/tracking replacement** — MLflow tags/aliases/gate flow and the content-addressed
  dataset registry are preserved as-is; score-at-registration is consumed, not modified.
- **No new modalities, no new fine-tune flows, no UI redesign** — the UI gains only policy
  declaration/visibility and the promotion-suggestion surface.
- **No default behavior change** — refuse-if-held stays the preempt default; `manual` stays the
  promotion default; every automation added here is opt-in per policy.
- **No auth model change** — single shared key + localhost binding remain; audit records are for
  automated actions, not a user/role system.
