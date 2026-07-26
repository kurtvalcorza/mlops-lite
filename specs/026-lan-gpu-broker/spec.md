# Feature Specification: LAN Self-Service GPU Broker

**Feature Branch**: `026-lan-gpu-broker`

**Created**: 2026-07-19

**Status**: Draft

**Input**: User description: "LAN self-service GPU broker for mlops-lite — a self-hosted, Brev-like multi-tenant GPU serving + jobs platform on a single GPU. Extend the existing host-agent admission lease into a broker that lets multiple same-LAN users/devices submit inference requests and training/batch jobs to the one GPU, with per-tenant auth, quotas, and GPU-seconds accounting."

## Clarifications

### Session 2026-07-19

- Q: FR-023 — Should the broker keep Principle II strict (one resident GPU tenant, multimodal via swap), or pursue bounded co-residency of multiple small models within a VRAM budget? → A: **Pursue co-residency.** Multiple *serving* models MAY be co-resident when their combined footprint fits a configured VRAM budget (checked against live free VRAM); larger models evict resident serving tenants to fit. This changes the platform's core invariant and therefore **REQUIRED a formal amendment to the NON-NEGOTIABLE Principle II** — ratified in constitution v1.6.0 (2026-07-19). Exclusive training/HPO/batch jobs still take the whole GPU (no co-residency during a job) and are never preempted.
- Q: What is the canonical unit for metering and quotas (GPU-seconds vs credits)? → A: **GPU-seconds is canonical.** Metering and quotas are stored and enforced in GPU-seconds; "credits" is only a user-facing display alias for GPU-seconds.
- Q: How does a tenant's quota replenish? → A: **Recurring-window reset.** Each tenant has a budget of GPU-seconds for a configurable window (e.g., daily/monthly) that auto-resets; no manual top-up is required for normal operation.
- Q: How should the GPU queue prioritize work under contention? → A: **Shape-based lanes + FIFO within a lane.** Inference is served/interleaved ahead of exclusive jobs; jobs run first-in-first-out; the owner may override ordering. A running exclusive job is still never preempted.
- Q: What isolation posture applies to tenant-submitted job workloads? → A: **Strong sandbox always.** Every batch/training job runs in a hardened sandbox runtime (isolated kernel/VM boundary, e.g., gVisor/Kata-class), non-root, with no host filesystem mounts and restricted network egress — no trusted-tenant relaxation.

### Session 2026-07-19 (Codex architecture review — corrections)

An independent architecture review (Codex/GPT-5, grounded in `hostagent/admission.py` + the constitution) found real defects; these are now folded into the spec/plan/contracts (constitution patched to v1.6.1):
- **VRAM invariant was mathematically wrong** — "combined footprint ≤ live free VRAM" double-counts residents (live-free already excludes them). Corrected to two distinct checks: accounted set + reservations ≤ **usable budget** (device total − safety reserve), AND each load ≤ **live free VRAM** − headroom, reconciled to the real post-load delta (FR-019, FR-023, FR-024).
- **Admission must not hold the GPU lock across model load/unload** — `admission.py` already documents this as an ABBA-deadlock lesson. The design is now a coordinator **reserve → load-outside-lock → commit/rollback** protocol; eviction drains (waits for in-flight requests) before unload (FR-023, contracts/admission-scheduler).
- **Metering can't record-before-work** — final GPU-seconds are unknown up front. Switched to idempotent **reserve-then-settle** (FR-016).
- **Single GPU-scheduling authority** — the host-agent coordinator owns all GPU ordering. `gateway/app/scheduler.py` is the 018 `PolicyScheduler` (drift/retrain monitoring), **not** a competing ordering authority, and is kept as-is; the real gap it created — tenant-less policy retrains calling `/train` outside any lane — is closed by routing them into the jobs lane under a reserved system tenant. The job queue is **persisted** (survives restart) and inference has a **bounded burst / job-drain mode** to prevent job starvation (FR-025).
- **Job sandbox is a new runtime + unproven on WSL2** — gated behind a feasibility spike; if it fails, arbitrary-tenant jobs (P2) ship as signed broker-owned recipes, move to native Linux, or are deferred — not as a weaker fallback pretending to comply (FR-026).
- **TLS required for multi-tenant** — plaintext bearer keys over the LAN are not an acceptable posture once tenants are independent (FR-002a).

## User Scenarios & Testing *(mandatory)*

This feature productizes the platform's existing single-GPU admission lease into a **multi-tenant, self-service broker** that other people and machines on the local network can use directly — without the owner mediating each request. The unifying idea: **many tenants, one GPU authority.** Every story funnels through the same GPU admission authority, which guarantees resident models never exceed the GPU's VRAM budget — one exclusive tenant for training/batch jobs, and bounded co-residency of small serving models when they fit (the latter pending the Principle II amendment; see Dependencies).

### User Story 1 - Private multi-tenant inference endpoint (Priority: P1)

A person or service on the LAN holds an API key. They point a standard inference client at the broker's address and send a chat or embedding request. Concurrent requests from several tenants against an already-resident model are interleaved and each gets a correct response. Every request is attributed to its tenant and its GPU usage recorded. No owner action is required per request.

**Why this priority**: This is the MVP and the highest-value slice — it lets the owner's own apps and LAN devices consume private, on-premise inference instead of paying a cloud provider, and it delivers value entirely on its own. It is fully compliant with Principle II (a single resident serving tenant shared by many callers).

**Independent Test**: Issue a tenant an API key, send inference requests from two LAN devices simultaneously against one resident model, confirm both receive correct responses, unauthorized requests are refused, and both requests appear attributed in usage records.

**Acceptance Scenarios**:

1. **Given** a tenant with a valid API key and a resident model, **When** they send an inference request over the LAN, **Then** they receive a correct response and the usage is recorded against their tenant.
2. **Given** two tenants sending inference requests at the same time against one resident model, **When** the requests arrive, **Then** both are served (interleaved) without either being dropped.
3. **Given** a request with a missing or invalid API key, **When** it reaches the broker, **Then** it is rejected and no GPU work is performed.
4. **Given** a request that arrives from outside the LAN, **When** it reaches the network boundary, **Then** it cannot reach the broker at all.

---

### User Story 2 - Submit-and-queue batch & training jobs (Priority: P2)

A tenant submits a job — a training/fine-tune run or a batch workload — to the broker rather than running it on their own machine. The broker checks the tenant's quota, places the job in a queue, and when the GPU is free grants it an **exclusive** lease. The job runs, streams logs back, produces artifacts (and, for a fine-tune, a registered model with lineage), then releases the GPU. A second job submitted while the first runs waits and starts automatically on release. GPU-seconds are metered to the tenant.

**Why this priority**: This is the second pillar of "self-service GPU" — offloading heavy work to the shared GPU — and it reuses the existing training pipeline and registry. It depends only on the admission lease, so it is independently testable.

**Independent Test**: Submit two jobs from different tenants; confirm the first gets the exclusive lease and runs to completion producing artifacts, the second waits then starts automatically, at no point are two GPU tenants resident, and each job's GPU-seconds are recorded.

**Acceptance Scenarios**:

1. **Given** an idle GPU, **When** a tenant submits a training job within quota, **Then** it acquires the exclusive lease, runs, emits artifacts/a registered model, and releases the GPU.
2. **Given** a job already holding the GPU, **When** a second job is submitted, **Then** it is queued and starts automatically when the first releases — with no manual intervention.
3. **Given** a running job, **When** the tenant requests its status/logs, **Then** they can see queue position (if waiting) or streamed logs (if running).
4. **Given** a running training job, **When** any other GPU request (inference or job) arrives, **Then** the running job is **never** preempted and the newcomer waits.

---

### User Story 3 - Quotas, usage ledger, and operator visibility (Priority: P3)

Each tenant has a quota — a budget of GPU-seconds (shown to users as "credits") for a recurring window that auto-resets. The broker records every GPU-consuming request and job to an append-only usage ledger and enforces the quota, refusing further GPU work once a tenant's window budget is exhausted until the window resets. The owner can see, in the operator console, current queue depth, which tenant is resident, and per-tenant usage.

**Why this priority**: This turns raw sharing into governed, accountable sharing (the "rent") and gives the owner control and visibility. It builds on the metering introduced in P1/P2.

**Independent Test**: Set a small quota for a tenant, drive usage past it, confirm subsequent GPU requests are refused while an in-quota tenant still succeeds, and confirm the ledger totals reconcile with the work performed and are visible in the console.

**Acceptance Scenarios**:

1. **Given** a tenant near its quota, **When** its usage crosses the limit, **Then** further GPU work for that tenant is refused until reset, while other tenants are unaffected.
2. **Given** completed inference requests and jobs, **When** the owner views the ledger, **Then** each entry is attributed to a tenant with recorded GPU usage and the totals reconcile with work performed.
3. **Given** an active queue, **When** the owner opens the console, **Then** they see current queue depth, the resident tenant, and per-tenant usage.

---

### User Story 4 - Additional serving modalities (ASR & computer vision) (Priority: P4)

Beyond text generation and embeddings, tenants can request speech-to-text (audio transcription) and classic computer-vision tasks (e.g., image classification/detection) through the same broker, each via an appropriate request format. Small serving models for different modalities MAY be **co-resident** and serve concurrently when their combined footprint fits the VRAM budget; when a requested model does not fit, the broker evicts resident serving tenants (per policy) to make room. A large model (e.g., a sizable LLM) may consume most of the budget and force others out.

**Why this priority**: It broadens the broker to the platform's already-validated modalities, but it is additive on top of the P1 serving path and therefore lower priority than establishing tenancy, jobs, and quotas.

**Independent Test**: With a small model resident and budget headroom, send an audio-transcription request whose model also fits; confirm the ASR model becomes co-resident and both modalities serve without evicting each other, and that both VRAM bounds hold throughout — the accounted set plus outstanding reservations never exceeds the usable budget, and each individual load fit live free VRAM minus unmaterialized reservations and headroom.

**Acceptance Scenarios**:

1. **Given** a small serving model resident with budget headroom, **When** a tenant sends an audio-transcription request whose model also fits, **Then** the ASR model becomes co-resident and returns a transcript without evicting the first model.
2. **Given** a computer-vision request, **When** it is served, **Then** the tenant receives the task-appropriate result (e.g., labels/scores or detections).
3. **Given** a requested model that does not fit alongside current serving tenants, **When** it is admitted, **Then** the broker evicts resident serving tenants per policy to make room, and the combined resident footprint never exceeds live free VRAM.

---

### User Story 5 - Interactive GPU sessions from a notebook (Priority: P5)

A tenant opens an interactive notebook session backed by the shared GPU to do exploratory work. Because such a session would otherwise hold the exclusive lease indefinitely, the broker auto-releases the GPU after a configured idle period and enforces a maximum session lifetime (TTL). For actual training from a notebook, the recommended path is to submit the run as a job (Story 2) so the GPU is held only during execution.

**Why this priority**: Interactive sessions are the most GPU-monopolizing shape on a single GPU, so they are the last, privileged, guard-railed addition.

**Independent Test**: Start an interactive session, leave it idle past the configured window, and confirm the GPU lease is released automatically; confirm a session exceeding its TTL is ended.

**Acceptance Scenarios**:

1. **Given** an interactive session holding the GPU, **When** it is idle beyond the configured window, **Then** the GPU lease is released automatically.
2. **Given** an interactive session, **When** it reaches its maximum lifetime (TTL), **Then** it is ended and the GPU freed.
3. **Given** a tenant wanting to train from a notebook, **When** they use the submit-as-job path, **Then** the GPU is leased only for the run's duration, not the whole session.

---

### Edge Cases

- **GPU busy with a job**: while a training/batch job holds the exclusive lease, inference requests for other tenants cannot be served immediately — they queue or are refused with a clear "GPU busy" signal (training is never preempted).
- **Quota exhausted mid-stream**: a tenant crossing its quota during a multi-step interaction is refused further GPU work with a clear reason; already-started work is allowed to finish.
- **Requested model too large for VRAM**: a request for a model that cannot fit the GPU's live free VRAM is refused with a clear reason rather than crashing.
- **Concurrent load requests**: two tenants requesting different models at the same instant are serialized by admission; each is placed if it fits the VRAM budget (becoming co-resident), otherwise one triggers eviction of resident serving tenants per policy — the combined footprint never exceeds live free VRAM, and no exclusive-job invariant is broken.
- **Key revoked mid-use**: a tenant whose key is revoked has in-flight work handled gracefully and subsequent requests refused.
- **Ledger write failure**: if usage cannot be recorded, the system fails safe (does not silently perform unmetered GPU work) per the platform's "if it isn't tracked, it didn't happen" principle.
- **Malformed / oversized job submission**: rejected at submission with a validation error, before touching the GPU.
- **Interactive session abandoned**: released by idle-cull/TTL so it cannot starve other tenants indefinitely.
- **Hostile / buggy job workload**: a submitted job that attempts to read the host filesystem, reach other tenants' data, or make disallowed network calls is contained by the sandbox — it cannot escape its isolation boundary, and such attempts do not affect other tenants or the host.
- **Inference arriving during a job**: an inference request that arrives while an exclusive job holds the GPU is queued in its lane (never preempting the job) and served when the GPU frees, or refused with a clear "GPU busy" signal per policy.

## Requirements *(mandatory)*

### Functional Requirements

**Access & tenancy**

- **FR-001**: The broker MUST be reachable by other devices on the same local network and MUST NOT be exposed to the public internet.
- **FR-002**: The broker MUST authenticate every request via a per-tenant credential (API key); requests without a valid credential MUST be refused and MUST perform no GPU work.
- **FR-002a**: Once more than one independent tenant exists, tenant traffic carrying API keys MUST be transport-encrypted (TLS); plaintext bearer keys over the LAN are not an acceptable multi-tenant posture. (A single-owner localhost deployment MAY run without TLS.)
- **FR-003**: The system MUST support multiple distinct tenants, each independently identified, credentialed, quota'd, and metered.
- **FR-004**: The owner MUST be able to create, and revoke, tenant credentials.

**Inference serving (P1)**

- **FR-005**: A tenant MUST be able to obtain text-generation and embedding inference from a resident model over the LAN using a standard inference request, without per-request owner involvement.
- **FR-006**: The broker MUST serve concurrent inference requests from multiple tenants against a single resident model by interleaving them, without dropping requests.
- **FR-007**: Every inference request that consumes the GPU MUST be recorded against its tenant with a measure of GPU usage.

**Jobs (P2)**

- **FR-008**: A tenant MUST be able to submit a batch or training/fine-tune job to the broker for execution on the shared GPU.
- **FR-009**: The broker MUST queue submitted jobs and grant each an **exclusive** GPU lease when the GPU is free, starting queued jobs automatically upon release with no manual step.
- **FR-010**: A running training/HPO/batch job MUST NOT be preempted by any other GPU request.
- **FR-011**: A fine-tune job MUST produce a registered model with recorded lineage, consistent with the platform's registry and evaluation gates; batch jobs MUST produce retrievable artifacts.
- **FR-012**: A tenant MUST be able to see a submitted job's queue position (while waiting) and streamed logs (while running), and its final status.
- **FR-013**: Job GPU usage MUST be metered in GPU-time and recorded against the submitting tenant.

**Quotas, ledger & visibility (P3)**

- **FR-014**: Each tenant MUST have a configurable quota expressed in **GPU-seconds** (surfaced to users as "credits," a display alias) covering a **recurring window** (e.g., daily/monthly) that auto-resets; the broker MUST enforce it, refusing further GPU work once the window's budget is exhausted until the window resets.
- **FR-015**: The system MUST maintain an append-only usage ledger attributing all GPU consumption (requests and jobs) to tenants, measured in **GPU-seconds**.
- **FR-016**: GPU usage MUST be **authorized before work via an idempotent reservation** (estimated/max GPU-seconds, keyed by request/job id) and **settled to the actual amount on completion**; if a reservation cannot be recorded the work MUST be refused (fail-safe), and settlement MUST be durable (retried via outbox/WAL if the store is briefly unavailable). Final-only recording is insufficient because usage is unknown before execution.
- **FR-017**: The owner MUST be able to view current queue depth, the resident GPU tenant, and per-tenant usage through the operator console.

**Modalities (P4)**

- **FR-018**: The broker MUST route a request to the correct serving behavior based on its task type (text generation, embeddings, speech-to-text, computer-vision).
- **FR-019**: Serving a modality whose model is not already resident MUST load it as a co-resident serving tenant when it fits; otherwise the system MUST evict resident serving tenants (idle-first/LRU) — **draining** each (stop new requests, wait for in-flight to finish) before unload — or refuse. The combined **accounted** footprint of resident serving tenants (plus outstanding reservations) MUST never exceed the **usable VRAM budget** (device total − safety reserve), and each individual load MUST additionally fit **live free VRAM** − headroom, reconciled to the actual post-load delta. The live-free bound MUST deduct **outstanding reservations whose load has not yet materialized**, so that concurrent admissions cannot each claim the same free bytes. A replacement reservation MUST NOT be recorded until any eviction it depends on has **actually completed** (drained, unloaded, and verified against NVML) and both bounds have been **re-derived** — marking a victim for eviction is not sufficient. Load/unload MUST NOT be performed while holding the GPU admission lock (reserve → load-outside-lock → commit/rollback), **including on the rollback path** — a stale-generation rollback MUST record intent under the lock, unload outside it, and reacquire only to finalize.

**Interactive sessions (P5)**

- **FR-020**: A tenant MUST be able to start an interactive GPU-backed session, subject to owner policy.
- **FR-021**: An interactive session MUST automatically release the GPU after a configurable idle period and MUST be ended at a configurable maximum lifetime (TTL).
- **FR-022**: The system MUST offer a path to run training from a notebook as a submitted job so the GPU is held only for the run's duration.

**Core invariant (VRAM-budget admission — amends Principle II)**

- **FR-023**: The system MUST permit multiple *serving* models to be co-resident when their combined accounted footprint (plus reservations) fits the **usable VRAM budget** AND the incoming load fits **live free VRAM** (with headroom). Admission/load/commit MUST follow a **reserve → load-outside-lock → commit-or-rollback** protocol (guarded by a per-model generation token) so the GPU admission lock is **never held across model load/unload** — reflecting the ABBA-deadlock lesson already recorded in `hostagent/admission.py`. Eviction to make room MUST drain a victim (no new requests; wait for in-flight to finish) before unload. *(Realized by the GPU coordinator — see contracts/admission-scheduler.md; enabled by constitution v1.6.1.)*
- **FR-023a**: A training / HPO / batch job MUST run with **exclusive** use of the GPU — no models co-resident for the duration of the job — and MUST NOT be preempted (reaffirms FR-010). Co-residency applies to serving tenants only.
- **FR-024**: Admission MUST enforce **both** bounds — accounted set + reservations ≤ usable budget, **and** incoming load ≤ live free VRAM − headroom — and MUST measure the actual VRAM delta after load, rolling back if an estimate drifted; a model that does not fit (alone, or after permitted eviction) MUST be refused with a clear reason rather than failing unsafely.

**Scheduling & isolation**

- **FR-025**: When multiple requests/jobs contend for the GPU, the broker MUST order them by **shape-based lanes with FIFO within each lane** — inference interleaved ahead of exclusive jobs, jobs FIFO — with owner override, and a running job never preempted. GPU ordering MUST have a **single authority** — the host-agent coordinator. Every path that can occupy the GPU MUST enter through it; in particular, policy-triggered retrains (018 `PolicyScheduler`, which has no tenant identity and today calls `/train` directly) MUST enqueue onto the jobs lane under a reserved **system tenant** rather than bypassing it. To prevent job starvation, after a configurable inference burst **or** head-job wait bound the broker MUST enter a **job-drain mode** that stops admitting NEW inference (running requests finish) until the head FIFO job acquires the GPU. The job queue MUST be **persisted** so ordering survives a host-agent restart.
- **FR-026**: Arbitrary tenant-submitted job code MUST execute inside a **hardened sandbox runtime** (isolated kernel/VM boundary, e.g., gVisor/Kata-class), non-root, no host mounts, restricted egress. **The feasibility spike (2026-07-19) confirmed this is NOT achievable on the current WSL2 host** — the GPU is paravirtualized (`/dev/dxg`, no `/dev/nvidia*`, no assignable PCI GPU), so gVisor `nvproxy` and Kata VFIO cannot function (see [spikes/sandbox-feasibility.md](./spikes/sandbox-feasibility.md)). **Decision: P2 arbitrary-job execution runs on a native-Linux GPU host** (real NVIDIA driver nodes) where the sandbox is validated, gated on that host migration **and** a new-runtime constitution amendment. P2 MUST NOT ship on WSL2 under a weaker rootless-namespace posture as if compliant. P1/P3/P4 are unaffected and remain on the current host.

### Key Entities *(include if feature involves data)*

- **Tenant**: A distinct consumer of the broker (a person, device, or service) with an identity, one or more credentials, a quota, and accumulated usage.
- **API Key / Credential**: A secret that authenticates requests as a given tenant; can be issued and revoked by the owner.
- **Quota**: A tenant's budget of **GPU-seconds** (aliased to users as "credits") for a recurring window (e.g., daily/monthly), with the current remaining balance and the window's reset schedule.
- **Usage Ledger Entry**: An append-only record attributing a unit of GPU consumption (from a request or a job) to a tenant, with the amount consumed **in GPU-seconds** and a timestamp.
- **Job**: A tenant-submitted unit of work (training/fine-tune or batch) with a definition, queue state, run state, logs, produced artifacts/registered model, and metered usage.
- **GPU Lease**: The single race-free claim on the GPU that guarantees one resident tenant at a time (existing platform primitive this feature builds on).
- **Serving Tenant (Resident Model)**: The model currently loaded and serving requests, by modality.
- **Interactive Session**: A tenant's live GPU-backed working session with idle and lifetime limits.
- **Queue**: The ordered set of pending GPU requests/jobs awaiting admission, ordered by **shape-based lanes with FIFO within each lane** (inference ahead of exclusive jobs), with an owner override.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A LAN tenant with a valid key can obtain an inference response with no owner action in the loop (fully self-service).
- **SC-002**: At least 5 concurrent inference requests from distinct tenants against one resident model all receive correct responses with none dropped.
- **SC-003**: 100% of requests lacking a valid credential are refused and cause no GPU work.
- **SC-004**: 100% of GPU-consuming requests and jobs appear in the usage ledger attributed to a tenant, and ledger totals reconcile with work performed within 5% — with "inference GPU-seconds" defined as busy-time under the resident child so the reconciliation is testable.
- **SC-005**: A tenant that exceeds its quota is refused further GPU work within one request of crossing the limit, while other tenants remain unaffected.
- **SC-006**: Across any sequence of mixed inference/job/session activity, monitoring shows the combined **accounted** VRAM of resident serving tenants never exceeds the **usable budget** and every load fit **live free VRAM** (admission refuses or drains/evicts to stay within both, never holding the lock across load/unload), during any exclusive training/batch job no other model is resident, and a queued job starts automatically within seconds of the GPU freeing.
- **SC-007**: An interactive session left idle releases the GPU within its configured idle window (e.g., ≤ the set number of minutes), and no session outlives its TTL.
- **SC-008**: The broker is confirmed unreachable from outside the LAN (no public route).
- **SC-009**: The owner can, from the console, identify current queue depth, the resident tenant, and each tenant's usage at a glance.
- **SC-010**: A running training job is never interrupted by an incoming request across the full test suite (zero preemptions of jobs).
- **SC-011**: A job workload attempting to access the host filesystem, other tenants' data, or disallowed network destinations is blocked by the sandbox in 100% of isolation tests, with no effect on other tenants or the host.
- **SC-012**: A tenant exhausted within a quota window regains its budget automatically at the next window reset, with no owner action required.

## Assumptions

- **Single GPU, single machine**: The target is the one machine/GPU in the platform hardware profile; there is exactly one GPU and no multi-node or cluster assumption (Principle I).
- **Reuses existing platform primitives**: The feature builds on the current GPU host-agent admission lease, the model registry + evaluation gates, the multimodal training pipeline (for fine-tune jobs), the relational store (for tenants/quotas/ledger), and the operator console (for visibility) — it does not re-implement these.
- **Trusted-ish LAN**: Tenants are on the owner's local network; access control is by broker-issued API key. **TLS is required once more than one independent tenant exists** (FR-002a) — a single-owner localhost deployment may run without it.
- **Interactive sessions are not serving models**: a notebook session has unpredictable, bursty VRAM allocation, so it is not treated as an ordinary co-resident serving tenant; P5 must decide whether a session is exclusive, a strongly-sandboxed job, or needs its own admission treatment (see Dependencies) — it is the lowest-priority shape and aggressively idle-culled regardless.
- **Internal accounting, not payment**: "Rent"/credits are an internal metering and quota mechanism; no real-money billing or payment processing is in scope.
- **Owner remote access is separate**: The owner's own remote access to their machines (e.g., via a personal VPN overlay) is out of scope for this feature and unrelated to tenant access.
- **Principle II amendment — DONE**: This feature introduces bounded co-residency of serving models within a VRAM budget. The required amendment to the NON-NEGOTIABLE Principle II has been ratified (constitution v1.6.0, 2026-07-19), so both planning and implementation are unblocked (see Dependencies).
- **Phase-gated delivery**: Stories ship in priority order (P1→P5); each must run end-to-end on the target hardware before the next, consistent with the platform's incremental delivery principle.

## Dependencies

- **Constitution amendment to Principle II — SATISFIED (constitution v1.6.1, 2026-07-19)**: Principle II permits **bounded co-residency of serving tenants** within a VRAM budget (v1.6.0), with the VRAM-accounting wording corrected in **v1.6.1** (usable-budget bound + per-load live-free bound; no double-count) after the Codex review. Preserves exclusive/never-preempted jobs. FR-019/FR-023/FR-024 co-residency is constitution-compliant and unblocked.
- **Native-Linux GPU host for P2 — GATED (blocks P2 arbitrary jobs only)**: The sandbox feasibility spike (2026-07-19) **confirmed gVisor/Kata GPU isolation is infeasible on WSL2** (paravirtualized GPU; no `/dev/nvidia*`; no PCI GPU for VFIO — see [spikes/sandbox-feasibility.md](./spikes/sandbox-feasibility.md)). **Decision:** P2 arbitrary-tenant jobs run on a **native-Linux GPU host** and are gated on THREE prerequisites: (1) migrating the GPU host agent to native Linux with a **re-run spike that passes** (gVisor `nvproxy` or Kata VFIO validated with a CUDA container); (2) a **new-runtime constitution amendment** (gVisor/Kata is a new runtime even on Linux); (3) P2 tasks authored but **BLOCKED** until (1)+(2) land. P1 (inference), P3 (co-residency), P4, quotas/ledger, and sessions' non-GPU parts do NOT depend on this and proceed on the current WSL host.
- **Interactive-session admission decision — PENDING (blocks P5 only)**: whether a session is exclusive, a sandboxed job, or a distinct admission class must be decided at P5 (may need its own amendment); does not block P1–P4.
- **Existing platform primitives (reused, not rebuilt)**: GPU host-agent admission (`hostagent/admission.py`), model registry + evaluation gates, the multimodal training pipeline (010) for fine-tune jobs, the relational store (Postgres) for tenants/quotas/ledger, and the operator console (021) for visibility.

## Out of Scope / Non-Goals

- **Multi-cloud GPU brokering** (renting GPUs from external cloud providers) — the platform owns its hardware.
- **Transparent remote-GPU virtualization** (making the GPU appear local on another machine, e.g., API-remoting/vGPU) — not viable on the target consumer GPU and against the clean-serialization design.
- **Multi-node / cluster scheduling** — deferred to established cluster schedulers if the platform ever grows beyond one node (Principle I).
- **VPN-overlay-based tenant access** (exposing the broker to tenants beyond the LAN) — LAN-only for this feature.
- **Concurrent dedicated GPUs for multiple heavy jobs** — a single GPU serializes exclusive work by design.
- **Real-money billing / payments** — only internal quota/credit accounting is in scope.
