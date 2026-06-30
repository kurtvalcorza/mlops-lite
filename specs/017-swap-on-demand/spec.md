# Feature Specification: Swap-on-Demand (preemptive GPU lease for serving)

**Feature Branch**: `017-swap-on-demand`

**Created**: 2026-06-30

**Status**: **DRAFT — GRILLED (2026-06-30), build-ready** (pending plan/tasks).

**Input**: The deferred **A2** fast-follow from 008. 008 shipped a **cooperative refuse-if-held** GPU lease:
when a tenant holds the GPU, another tenant is refused (the Infer tab disables `classify` with a hint —
008's A1). 008 explicitly deferred **A2 (swap-on-demand)** — "evict the holder, load the requested model
(~2.5s), behind a cost-stating confirm" — as a documented fast-follow that would upgrade the lease to
**preemptive**. 017 builds A2: an **operator-confirmed, serving-only preemptive swap** so an operator can,
e.g., classify an image while the LLM is resident (today: blocked) by evicting the resident model and
loading the target — without ever interrupting a running training job.

> **Grilled decisions (2026-06-30):**
> 1. **Preemptable = serving tenants only** (LLM / vision / ASR — a resident model, cheap to reload). A
>    **running training / HPO / batch run is NOT preemptable** — evicting it mid-run destroys GPU work; it
>    keeps the cooperative **refuse-if-held** semantics. Swap-on-demand swaps *served models*, never
>    interrupts active training.
> 2. **Orchestration = gateway-brokered via an `unload-now` endpoint** on each preemptable serving daemon.
>    Flow: confirm → gateway sends `unload-now` to the current holder → waits for the lease to free →
>    forwards the request to the target daemon (which acquires + loads). The gateway already knows the
>    holder (`/serving/state`) and fronts the confirm; daemons just add an unload endpoint. (Not
>    daemon-to-daemon — that couples every serving daemon to every other; not a lease-poll flag — extra
>    latency + a new lease state machine.)
> 3. **In-flight handling = drain with a bounded timeout.** On `unload-now` the holder finishes its current
>    in-flight request(s), then unloads + releases; a request exceeding the timeout falls back to a hard
>    unload (so a stuck request can't hang the swap). No dropped work in the common case.
> 4. **Request = a per-request opt-in flag (`preempt`/`swap=true`), default OFF.** Refuse-if-held stays the
>    default **everywhere** (API / CLI / UI) — preemption is never implicit or surprising. The capability
>    is uniform (API/CLI can opt in too), the UI just sets the flag after its confirm.
> 5. **UX (consequence of 4) = the Infer tab's A1 "classify disabled — GPU busy" becomes a "Swap &
>    classify" action** that states the cost ("evicts the resident LLM, ~2.5s reload") and, on confirm,
>    sends the request with `preempt=true`.

> **Scope note**: 017 **extends 008's lease behavior** (Principle II) from cooperative-only to
> cooperative + **operator-confirmed preemptive (serving)**. It adds **no new always-on service, no new
> runtime**: an `unload-now` endpoint on the existing serving supervisors + gateway orchestration + a UI
> confirm. Requirement IDs continue the shared space (FR-154+, SC-100+, tasks T324+). **Constitution: very
> likely no amendment** — Principle II ("one GPU tenant under a single race-free lease") is **preserved**
> (still exactly one resident model; the swap is sequential evict→free→load); but 008's v1.4.0 text
> described the lease as cooperative with "no swap/evict", so the plan's Constitution Check MUST confirm
> whether a one-line wording update is warranted. See plan.md → Constitution Check.

> **Hard boundary (NON-NEGOTIABLE)**: **Principle II — one model in VRAM at any instant** is preserved:
> the swap is **sequential** (evict the holder → lease frees → load the target) — **never two models
> resident**. **Training is never preempted** (grilled decision 1). The **frozen GPU stack** is untouched
> and **no heavy new dependency** is added (Principle III) — the unload path reuses each supervisor's
> existing `_unload` + the 008 `gpu_lease` release.

> **Builds on**: 008 GPU lease (cooperative, v1.4.0) — this is its A2 fast-follow · 009 modality routing.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Operator swaps a resident serving model on demand (Priority: P1)

An operator wants to run a serving request (e.g. classify an image) while a different serving model (e.g.
the LLM) is resident. With an explicit, confirmed `preempt=true`, the gateway evicts the resident model
(drain → unload → release) and the target model loads + serves — a single sequential swap, never two
models resident, never touching a running training job.

**Why this priority**: This is A2 — the whole feature. It removes the 008/A1 dead-end where an operator
must manually idle-out or stop the holder to use another serving modality.

**Independent Test**: With the LLM resident, issue a classify with `preempt=true` → the LLM is evicted, the
vision model loads + returns a result; `nvidia-smi` shows the swap (LLM frees before vision loads) — never
two resident.

**Acceptance Scenarios**:

1. **Given** the LLM is resident and `preempt=true`, **When** a vision classify is requested, **Then** the
   gateway sends `unload-now` to the LLM holder, waits for the lease to free, and the vision model loads +
   serves — one model in VRAM throughout.
2. **Given** `preempt` is omitted/false and a model is resident, **When** another serving request arrives,
   **Then** behavior is **unchanged** from 008 — refuse-if-held (no swap).
3. **Given** a swap is in progress, **When** a second swap is requested, **Then** swaps serialize on the
   lease — never two concurrent evictions racing.

---

### User Story 2 — Training is never preempted (Priority: P1, guardrail)

A running training / HPO / batch job holds the GPU. A serving request with `preempt=true` MUST **refuse**
to evict it (refuse-if-held), with a clear message — long-running GPU work is never destroyed by a swap.

**Why this priority**: This is the safety boundary that makes preemption acceptable. Without it, an operator
could evict an hour-long fine-tune. It must be impossible.

**Independent Test**: Start a training run (holds the lease); request a serving swap with `preempt=true` →
refused with a clear "training in progress — not preemptable" message; the training run is unaffected.

**Acceptance Scenarios**:

1. **Given** a training/HPO/batch job holds the lease, **When** a serving request with `preempt=true`
   arrives, **Then** it is refused (the job is **not** evicted) with a clear message.

---

### User Story 3 — In-flight requests drain, not dropped (Priority: P2)

When the holder is told to unload while mid-request, it **finishes the in-flight request(s)** then unloads;
only a request exceeding a bounded timeout is hard-cut. Callers mid-request aren't silently dropped.

**Why this priority**: Graceful eviction is what makes preemption safe to expose; it's the difference
between a clean swap and surfacing errors to whoever was mid-inference.

**Independent Test**: Issue a long inference on the holder, then a `preempt=true` swap → the in-flight
inference completes (or is cut only after the timeout); the swap then proceeds.

**Acceptance Scenarios**:

1. **Given** the holder has an in-flight request, **When** `unload-now` arrives, **Then** it drains the
   request (up to the timeout) before unloading; past the timeout it hard-unloads.

---

### Edge Cases

- **Holder dies during the swap**: the lease's existing stale-reclaim (008 — dead PID → reclaim) frees it;
  the swap proceeds (no deadlock).
- **Target fails to load after eviction**: the GPU is now free but the target errored → return the load
  error; the operator can retry/serve the previous model (don't leave a wedged half-state).
- **`unload-now` to a daemon that isn't actually the holder** (stale `/serving/state`): no-op + re-resolve
  the real holder; never unload the wrong tenant.
- **Rapid repeated swaps (thrash)**: each swap is operator-confirmed + on-demand, which bounds thrash; v1
  adds **no** extra cooldown (operator confirmation is the guard) — revisit if abused.
- **CPU modalities (embeddings/tabular)** are off-lease → never involved in a swap.
- **Concurrent `preempt=true` requests**: serialize on the lease (US1 AS-3).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-154**: A serving request MAY carry an explicit **`preempt` (default false)** opt-in; with it set and
  a **serving** model resident, the platform performs a **swap** (evict holder → load target). Default
  (omitted/false) preserves 008 **refuse-if-held** exactly.
- **FR-155**: A **running training / HPO / batch** job MUST **never** be preempted — a `preempt=true`
  serving request against a training holder is **refused** with a clear message (FR boundary for US2).
- **FR-156**: Each **preemptable serving daemon** (LLM / vision / ASR supervisors) MUST expose an
  **`unload-now`** control endpoint that **drains in-flight request(s) within a bounded timeout**, then
  unloads its model and **releases the lease** (reusing its existing `_unload` + `gpu_lease.release`).
- **FR-157**: The **gateway** MUST orchestrate the swap: identify the holder (`/serving/state`), call
  `unload-now`, **wait for the lease to free**, then forward the request to the target daemon — without the
  gateway itself holding the lease.
- **FR-158**: The swap MUST be **sequential** — **one model in VRAM at any instant** (Principle II);
  concurrent `preempt=true` requests serialize on the lease.
- **FR-159**: If the target **fails to load** after eviction, the platform MUST surface the **load error**
  (not a silent wedged state); a stale-holder death during a swap is handled by 008's existing reclaim.
- **FR-160**: The Infer tab MUST replace the 008/A1 "classify disabled — GPU busy" state with a
  **cost-stating "Swap & classify"** confirm that sends `preempt=true` on confirm; the BFF allowlist passes
  the flag.
- **FR-161**: 017 MUST add **no new always-on service, no new runtime, and no heavy dependency**, MUST NOT
  move the frozen GPU stack, and MUST leave the **default (non-preempt) behavior byte-for-byte unchanged**.

> **[OPEN — for plan/tasks]** The drain timeout default + how a supervisor detects "in-flight" (an active
> request counter vs a llama-server health probe); whether `unload-now` is authenticated like other control
> routes; the exact gateway wait-for-free mechanism (poll `current_holder()` vs a short lease-free wait).

### Key Entities

- **Preempt request**: a serving request with `preempt=true` — the explicit opt-in that authorizes a swap.
- **Unload-now command**: a gateway→holder control call that drains + unloads + releases the lease.
- **Swap**: the sequential evict-holder → free → load-target operation, arbitrated by the single lease.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-100**: With `preempt=true` and a resident **serving** model, a different serving request succeeds by
  swapping — and **`nvidia-smi` never shows two models resident** during the swap (SC for Principle II).
- **SC-101**: With `preempt` omitted/false, behavior is **identical to 008** (refuse-if-held) — no
  regression, no surprise preemption.
- **SC-102**: A `preempt=true` serving request against a **training** holder is **refused**; the training
  run completes **unaffected** (never evicted).
- **SC-103**: An in-flight request on the holder **drains** (completes) before the swap, except past the
  bounded timeout (then hard-cut) — no silent drops in the common case.
- **SC-104**: The Infer tab offers a cost-stating **Swap & classify** that works end-to-end (evict LLM →
  classify) on confirm.
- **SC-105**: The frozen GPU stack + dependency footprint are unchanged; 001–016 suites still green.

## Assumptions

- The single GPU lease (008 `gpu_lease.py`) remains the sole VRAM-admission point; the swap routes through
  its existing acquire/release — preemption is the gateway driving an `unload-now` then a normal acquire.
- Each serving supervisor can detect/drain in-flight requests and already has an `_unload` path.
- Operator confirmation (the cost-stating UI) is the thrash guard for v1; no automatic/periodic preemption.

## Non-Goals

- **No preemption of training / HPO / batch** (grilled decision 1).
- **No implicit/default preemption** — `preempt` is explicit, default off (FR-154).
- **No automatic/scheduled swapping** — operator-initiated only (no scheduler).
- **No co-residency / two models in VRAM** (Principle II) — the swap is sequential.
- **No cross-tenant priority system** — a single explicit opt-in, not a scheduler/priority queue.
- **No change to the cooperative default** — 008 refuse-if-held remains the behavior without `preempt`.
