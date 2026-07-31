# Phase 0 Research — LAN Self-Service GPU Broker

Resolves the unknowns in the plan's Technical Context. Each decision is constrained by the constitution
(v1.6.0) and the existing architecture (host agent = sole GPU authority; slim engine children; Postgres
store; MLflow registry; Next.js console).

---

## R1 — Co-residency admission & eviction  *(revised after Codex review)*

**Decision**: **Replace** single-slot `admission.py` with a **GPU coordinator state machine** (`coordinator.py`)
— a resident *set* with per-model lifecycle (`loading/resident/draining/evicting`), active-request ref-counts,
per-model generation tokens, and one `exclusive_job` slot. Admission uses a **three-stage protocol**: (1) under
the lock, reserve capacity and mark state — **no I/O**; (2) load/unload the child **outside the lock** in the
established order; (3) re-enter the lock to commit or roll back via the generation token. **The lock is never
held across load/unload** — this is the ABBA-deadlock lesson already recorded in `admission.py:97`. Eviction
**drains** a victim (stop new requests; wait `active_requests==0`) before unload, so in-flight requests are
never corrupted. VRAM admission uses **two distinct bounds** (see R-VRAM below), not `Σ ≤ live_free`.
Resource identity is the **model instance, not the tenant** — tenants sharing a model share one child.

**R-VRAM (corrected accounting)**: `usable_capacity = min(configured_budget, NVML_total − safety_reserve)`;
admit iff `accounted_resident + reservations + requested ≤ usable_capacity` **AND**
`requested ≤ live_free − safety_headroom`; measure the real VRAM delta after load and commit/roll back the
reservation. (The v1.6.0 `Σ resident + requested ≤ live_free` double-counted residents — live-free already
excludes them; fixed in constitution v1.6.1.)

**Rationale**: Implements amended Principle II correctly and safely; preserves the single race-free critical
section for *state* while doing I/O outside it (no deadlock, no mid-request eviction).

**Alternatives considered**: extend the single-slot lease (original plan) — rejected: reintroduces the
documented ABBA deadlock and unsafe eviction. Static partitions / MIG — rejected (waste; unavailable on the
consumer GPU). Swap-only — rejected (pre-amendment behavior).

---

## R2 — Serving surface & engines (reuse, don't add vLLM/Triton)

**Decision**: Reuse the **existing engine children** (llm via llama.cpp, vision, ASR/whisper, embeddings,
tabular) behind the host agent. Add an **adapter surface at the gateway** that speaks OpenAI-compatible
routes (`/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/transcriptions`) and a task-typed CV endpoint
(`/v1/vision/{classify,detect}`), translating each to the corresponding child's existing `/infer` contract.
Concurrent tenant requests interleave against a resident child.

**Rationale**: The modalities are already served on this platform (009/010/022). Adding vLLM and Triton would
introduce heavy always-resident engines and new runtime deps — a **Principle III** (footprint/disk) breach —
for capability the platform already has. An OpenAI-compatible adapter is a thin interface (Principle V).

**Alternatives considered**: vLLM (chat) + Triton (CV) + faster-whisper as separate engines — rejected for
now on footprint/complexity; recorded as a **future swap** behind the same gateway interface if a child's
throughput proves insufficient. This keeps lock-in out (Principle V) without paying the cost now.

---

## R3 — Multi-tenancy & authentication

**Decision**: **Per-tenant API keys** issued by the owner, verified at the **gateway** (`gateway/app/tenancy`)
as `Authorization: Bearer <key>`. A key hashes to a stored record → `tenant_id`. The gateway attaches the
resolved tenant to the request context for quota + metering. Keys are revocable (soft-delete + deny). The host
agent's existing `auth.py` (agent↔gateway trust) is unchanged; tenant identity lives at the gateway only.

**Rationale**: Matches the LAN-only, key-based model (FR-002/003/004); keeps the agent's internal trust
boundary intact; no external IdP needed on a LAN.

**TLS is MANDATORY for multi-tenant deployments** (FR-002a, T626) — not the "optional transport nicety"
this decision originally called it. A bearer key is a *reusable* credential replayed on every request:
over plaintext HTTP any device on the LAN can capture one and impersonate that tenant indefinitely. A
shared LAN is precisely a network with observers you do not control — that is the threat this feature
introduces by opening the GPU to independent tenants. Plaintext `http://` is acceptable **only** for the
single-owner localhost deployment, where there is no second tenant to impersonate and no LAN hop.
Documented examples MUST use `https://` wherever a key is sent (see [user-guide.md](./user-guide.md)).

**Alternatives considered**: mTLS per tenant — rejected: heavier cert management for a home LAN; bearer
keys **over TLS** are sufficient. Reusing tailnet identity — out of scope (LAN-only decision).

---

## R4 — Metering (GPU-seconds)

**Decision**: **GPU-seconds is canonical** (credits = display alias). Two capture paths, both emitted by the
host agent (authoritative for GPU time) via `journal.py`/`metrics.py` and persisted to `usage_ledger`:
- **Inference**: GPU-seconds = busy-time under the resident child per request (this definition makes SC-004's
  5% reconciliation testable), attributed to the calling tenant.
- **Jobs**: GPU-seconds = exclusive-lease duration.

**Reserve → settle (revised after Codex review)**: usage cannot be "recorded before work" — the final amount
is unknown up front. Instead: (1) **reserve** an estimated/max charge, keyed idempotently by request/job id,
and refuse if the reservation can't be recorded or would exceed quota; (2) do the work; (3) **settle** the
actual GPU-seconds to the ledger and **release** the unused reservation. Settlement is durable — retried via
an **outbox/WAL** if Postgres is briefly unavailable after work has started. Prometheus counters mirror the
ledger.

**Rationale**: Fail-safe authorization + exactly-once settlement; append-only final-entry alone can't
pre-authorize or handle retries. Satisfies Principle VI.

**Alternatives considered**: record-before-work (impossible — usage unknown); token-based metering
(non-uniform across modalities).

---

## R5 — Quota model (recurring window)

**Decision**: Each tenant has a `quota` row: `window` (e.g., `daily`/`monthly`), `budget_gpu_seconds`, and a
derived current-window `consumed`/`remaining` computed from the ledger (window start = truncate(now, window)).
The gateway checks `remaining > 0` (and, for jobs, an estimated fit) at admission; on exhaustion it refuses
with `403 quota_exhausted` until the next window boundary auto-resets (no stored balance to top up; reset is
implicit via window truncation). Owner may raise/lower the budget any time.

**Rationale**: Recurring-window auto-reset is the most self-service-friendly (clarify decision); deriving
consumption from the append-only ledger avoids a mutable-balance race.

**Alternatives considered**: mutable credit balance with manual top-up — rejected by clarify; also a
concurrency hazard (read-modify-write on balance).

**Superseded (PR #74 review).** This decision originally deferred per-request hard reservation, calling a
"soft check at admission adequate at LAN scale." That is **no longer the design** and contradicted R4,
`data-model.md`'s `usage_reservation` entity, and FR-016, all of which mandate a hard, atomic,
reject-on-overflow reservation settled to actual on completion. R5 was not updated when R4 was revised
after the first Codex pass. **The hard reserve→settle reservation is authoritative**; an implementer must
not build the soft check described above.

---

## R6 — Scheduler: shape lanes + FIFO

**Decision**: A `hostagent/scheduler.py` fronts the coordinator with **two lanes** — an **inference lane**
(admitted against the budget, interleaved) and a **jobs lane** (exclusive, strict **FIFO**, **persisted in
Postgres** so ordering survives a host-agent restart). Inference is favored *but bounded*: after a
configurable inference **burst** OR a head-job **wait bound**, the coordinator enters **job-drain mode** —
it stops admitting NEW inference (running requests finish, never preempted) until the head FIFO job acquires
the GPU. This actually **prevents starvation** (a mere "starved" warning, in the v1 design, did not). The
**host-agent coordinator is the SOLE GPU-ordering authority**.

**Correction (PR #74 review).** An earlier revision of this decision claimed `gateway/app/scheduler.py`
was a competing GPU-ordering authority to be "audited and reduced to a status/routing facade." That was
a misidentification, confirmed independently by both reviewers and by reading the file: it is
`PolicyScheduler` from feature 018 — a drift/quality monitoring tick loop that launches retrains and
parks a `PendingRetrain` on `409 Busy` (FR-182). It has no lane ordering, no VRAM admission, and no
cross-tenant queue. Reducing it to a facade would delete the monitoring→retrain feedback loop while
consolidating no scheduling. **It is kept as-is.**

The genuine split-ownership risk it *does* create is different: `PolicyScheduler` calls the host
agent's `/train` directly, outside any lane, with no tenant identity. Under this design those
retrains **enqueue onto the jobs lane under a reserved system tenant**, so they queue FIFO by arrival,
honour the single `exclusive_job` slot, and are metered rather than invisible. Its existing
`Busy`/park-and-retry path is unaffected — a full jobs lane returns the same `409` it already handles.
Owner override may pin/pause/reorder queued jobs.

**Rationale**: Matches the clarify decision while fixing the two Codex findings — job starvation and split
scheduler ownership — and surviving restarts.

**Alternatives considered**: strict global FIFO (blocks inference); per-tenant fair-share (complex) — both
deferred. In-memory queue — rejected: lost on restart.

---

## R7 — Job sandbox on the WSL GPU host (feasibility-gated)

**Decision (revised after Codex review): make the sandbox a RELEASE GATE, not a fallback choice.** Two hard
facts: (a) a gVisor/Kata runtime is a **new runtime**, which the constitution forbids "without amendment";
(b) WSL2 exposes the GPU via a **paravirtualized** path, **not** ordinary PCI/VFIO passthrough — so Kata GPU
passthrough and gVisor `nvproxy` are **not** interchangeable drop-ins and must be proven end-to-end on the
exact WSL/driver/CUDA stack. Therefore, **before P2 (arbitrary-job execution) is implemented**, a
**feasibility spike** must prove isolated-kernel/VM GPU isolation actually works here, **and** a **runtime
amendment** must be ratified. If the spike **fails**, P2 does **NOT** ship the rootless-OCI posture as if
compliant (it does not satisfy FR-026); instead choose one:
1. accept only **broker-owned, signed job recipes** (not arbitrary tenant code) — no strong sandbox needed;
2. run the GPU host on **native Linux** with a validated Kata/gVisor stack;
3. **defer** arbitrary-job execution from this feature.

**Rationale**: The original "documented-deviation fallback" was unsound — rootless namespaces ≠ the mandated
isolation boundary. Gating is honest and keeps P1/P3/P4 moving regardless.

**Alternatives considered**: ship rootless-OCI as "degraded compliance" — rejected (misrepresents isolation).
Full VM per job — heavy; folded into option 2.

**SPIKE OUTCOME + DECISION (2026-07-19)**: the feasibility spike **failed on WSL2** — the GPU is
paravirtualized (`/dev/dxg`; no `/dev/nvidia*`; no PCI GPU for VFIO), so neither gVisor `nvproxy` nor Kata
VFIO can function (evidence: [spikes/sandbox-feasibility.md](./spikes/sandbox-feasibility.md)). **Owner chose
option 2 — native-Linux GPU host.** P2 keeps arbitrary-tenant jobs with the mandated sandbox, but runs on a
native-Linux host (real NVIDIA driver nodes) and is gated on: (1) the host migration + a passing re-run of the
spike, (2) a new-runtime constitution amendment. P1/P3/P4 continue on the current WSL host unblocked.

---

## R8 — LAN reachability from WSL

**Decision**: Bind the gateway to the LAN interface and make it reachable via **WSL mirrored-networking mode**
(`.wslconfig networkingMode=mirrored`, Win 11 22H2+) as the preferred path, with **`netsh portproxy`** (the
established `gui-remote-proxy` pattern) as the fallback when mirrored mode is undesirable globally. A
**DHCP-reserved** host IP (or mDNS `gpu.lan`) gives tenants a stable address. Router port-forwarding is
explicitly prohibited (FR-001).

**Rationale**: Mirrored mode is the cleanest (WSL shares host networking, LAN reaches services directly);
portproxy is zero-risk and already proven on this machine.

**Alternatives considered**: exposing via the owner's Tailscale/Headscale — out of scope (LAN-only decision).

---

## R9 — Interactive sessions

**Decision**: A session is a special **serving-class lease** with an **idle-cull** (release GPU after N
minutes of 0% GPU activity) and a hard **TTL**. Under co-residency it occupies budget like any serving tenant
but, because interactive use is bursty and unpredictable, it is **culled aggressively** and is the lowest
priority to keep resident under contention. Training from a notebook is steered to the **submit-as-job** path
(FR-022) so the GPU is held only during the run; an optional `%%gpu`/`broker.finetune()` helper wraps job
submission from a cell. Hosting mechanism (JupyterHub vs a minimal kernel gateway) is chosen at P5 build time;
the contract is the session lease + idle/TTL, not the notebook UI.

**Rationale**: Directly implements FR-020/021/022 and the single-GPU guard rails; keeps the greediest shape
boxed.

**Alternatives considered**: sessions holding an exclusive lease (like jobs) — rejected: needlessly blocks
inference; co-resident-with-cull is friendlier and still bounded.

---

## Resolved unknowns summary

| Unknown (Technical Context) | Resolution |
|---|---|
| Co-residency admission mechanism | R1 — VRAM-budget set in the existing lock; idle/LRU eviction |
| Which serving engines | R2 — reuse existing children; OpenAI adapter; no vLLM/Triton |
| Tenant auth | R3 — gateway per-tenant API keys |
| Metering unit + capture | R4 — GPU-seconds, agent-emitted, ledger on admission path |
| Quota model | R5 — recurring window, consumption derived from ledger |
| Scheduler policy | R6 — inference lane + jobs FIFO lane + owner override |
| Job isolation | R7 — spike complete: gVisor/Kata infeasible on WSL2; P2 gated on a native-Linux GPU host + runtime constitution amendment (the earlier "documented fallback" was rejected as unsound) |
| LAN reachability | R8 — mirrored-net (preferred) or portproxy; DHCP-reserved IP |
| Interactive sessions | R9 — serving-class lease, idle-cull + TTL, notebook-as-job |

No `NEEDS CLARIFICATION` markers remain. One **flagged risk**: R7 sandbox GPU passthrough on WSL — spiked in
Phase 1 before P2 job execution is built.
