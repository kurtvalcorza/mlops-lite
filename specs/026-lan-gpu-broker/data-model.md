# Phase 1 Data Model — LAN Self-Service GPU Broker

Persistent entities live in the Postgres relational store (via `platformlib` store + a new migration).
Runtime-only state (resident serving set, lease) lives in the host agent process. GPU-seconds is the
canonical usage unit throughout.

## Persistent entities (Postgres)

### tenant
| Field | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `name` | text unique | human label (e.g., `alice`, `vercel-chatbot`) |
| `status` | enum(`active`,`disabled`) | disabled → all keys refused |
| `created_at` | timestamptz | |

### api_key
| Field | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK→tenant | |
| `key_hash` | text | hash of the bearer secret; raw shown once at creation |
| `prefix` | text | short non-secret prefix for display/lookup |
| `status` | enum(`active`,`revoked`) | revoked → deny |
| `created_at` / `revoked_at` | timestamptz | |

**Validation**: a request authenticates iff `api_key.status='active'` AND `tenant.status='active'`.

### quota
| Field | Type | Notes |
|---|---|---|
| `tenant_id` | uuid FK→tenant (unique) | one quota per tenant |
| `window` | enum(`daily`,`weekly`,`monthly`) | recurring window |
| `budget_gpu_seconds` | bigint | budget per window |
| `updated_at` | timestamptz | |

**Derived (not stored)**: `window_start = date_trunc(window, now())`;
`consumed = Σ usage_ledger.gpu_seconds where ts ≥ window_start`; `remaining = budget − consumed`.
Auto-reset is implicit at each window boundary.

### usage_ledger  *(append-only)*
| Field | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `tenant_id` | uuid FK→tenant | |
| `kind` | enum(`inference`,`job`,`session`) | |
| `ref_id` | text | request id / job id / session id |
| `modality` | text | `chat`,`embeddings`,`asr`,`vision`,`train`,… |
| `gpu_seconds` | numeric | consumption (canonical unit) |
| `ts` | timestamptz | |

**Rule (FR-016)**: usage is **reserved before work** (see `usage_reservation`) and the final amount **settled**
here on completion; a GPU op is admitted only if its reservation can be written; write-fail ⇒ refuse.

### usage_reservation  *(idempotent pre-authorization — added per Codex review)*
| Field | Type | Notes |
|---|---|---|
| `op_id` | text PK | request id / job id (idempotency key) |
| `tenant_id` | uuid FK→tenant | |
| `window_start` | timestamptz | **the window this reservation is charged against** — see rule below |
| `est_gpu_seconds` | numeric | estimated/max charge held against quota before work |
| `state` | enum(`reserved`,`settled`,`released`) | settled → a `usage_ledger` row written; released → unused |
| `created_at` / `settled_at` | timestamptz | |

**Rule**: reserve (atomic, rejects if it would exceed the window budget) → do work → settle actual to
`usage_ledger` + release remainder. Durable settlement via outbox/WAL if the store is briefly unavailable.

**Window binding.** `window_start` is stamped at *reserve* time and reserve/settle/release all charge
**that** window — never the window in force at completion. A queued job reserved near a boundary can
finish after the reset; without the stored window its authorization is checked against the old window
while its ledger row lands in the new one, and the tenant can meanwhile reserve the new window's full
budget before the old job settles — overshooting it. Consumption for a window is therefore derived
from reservations and ledger rows **bearing that `window_start`**, not from completion timestamps.

### job
| Field | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK→tenant | |
| `kind` | enum(`batch`,`finetune`,`hpo`) | |
| `spec` | jsonb | image/entrypoint or finetune spec (base, data ref, params) |
| `state` | enum(`queued`,`running`,`succeeded`,`failed`,`cancelled`,`interrupted`) | `interrupted` = the broker lost the job (host-agent restart), **not** a tenant-code failure — see restart recovery |
| `queue_pos` | int null | while queued |
| `sandbox` | text | resolved sandbox runtime (kata/runsc/fallback) |
| `artifact_ref` / `model_version` | text null | outputs; finetune → MLflow version |
| `gpu_seconds` | numeric null | filled on completion (= lease duration) |
| `created_at`/`started_at`/`ended_at` | timestamptz | |

**State transitions**: `queued → running → (succeeded|failed)`; `queued → cancelled`;
`running → cancelled` (tenant/owner); `running → interrupted` (restart recovery only — never a
tenant-visible action). `running` is never forced to `queued` (no preemption).

**Restart recovery (FR-025).** Persisting the lane is not by itself enough for FIFO to survive a
restart: the existing host-agent startup path (`hostagent/journal.py`) atomically rewrites **every**
`queued` and `running` job to `interrupted`, which would silently empty the lane on every boot. Broker
jobs are therefore recovered explicitly rather than swept:

- `queued` jobs **stay `queued`, in their original `queue_pos` order** — they never occupied the GPU,
  so there is nothing to reconcile.
- The single formerly-`running` job is resolved to **`interrupted`** — never silently re-queued, since
  its sandbox is gone and re-running it is the tenant's decision, not the broker's.

  *Decided rather than left open:* the enum above now carries `interrupted` as a distinct terminal
  state. Collapsing it into `failed` would make every broker-caused restart indistinguishable from a
  genuine tenant-code failure — and in a metered multi-tenant broker that difference is the tenant's
  basis for disputing a charge, so it cannot be inferred from logs after the fact. `interrupted` is
  already the vocabulary `hostagent/journal.py` uses for exactly this event, so the broker stores the
  same word rather than a second one. It diverges from `hostagent/jobs.py`'s legacy surface, which maps
  `interrupted → status:failed` for the pre-broker API; that mapping stays as-is for that surface, and
  the broker's own `/jobs/{id}` reports `interrupted` verbatim (see
  [contracts/jobs-api.md](./contracts/jobs-api.md)).
- Its `usage_reservation` is **settled to elapsed GPU-seconds and the remainder released** against its
  stored `window_start`. Left `reserved`, it would hold quota against the tenant forever.
- `exclusive_job` and the resident set start empty; VRAM accounting is rebuilt from NVML, not restored.

### session
| Field | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK→tenant | |
| `state` | enum(`active`,`idle`,`released`,`expired`) | |
| `idle_timeout_s` / `ttl_s` | int | guard rails |
| `last_gpu_activity_at` | timestamptz | **drives idle-cull** — set only by GPU work the coordinator admitted |
| `last_heartbeat_at` | timestamptz | session/kernel liveness only; never resets the GPU idle timer |
| `created_at`/`ended_at` | timestamptz | |

**Transitions**: `active ⇄ idle`; `idle →(idle_timeout)→ released`; `active/idle →(ttl)→ expired`.

**Why two timestamps.** A single `last_activity_at` fed by both signals lets a notebook's automatic
liveness heartbeat hold the GPU for the full TTL while running nothing — the abandoned-session case
SC-007 exists to bound. Idle-cull keys on `last_gpu_activity_at` alone; see
[contracts/sessions-api.md](./contracts/sessions-api.md).

## Runtime state (host agent, in-process — not persisted)

### GPU coordinator state  *(state machine — corrected per Codex review)*
- `residents: model_key → { state: loading|resident|draining|evicting|rolling_back, vram_accounted_bytes, active_requests, last_used_at }`
  — `rolling_back` is the record-intent state of the split rollback; a model in any transient state
  (`draining`/`evicting`/`rolling_back`) is **not** eligible as a fresh-load target or an eviction victim.
- `reservations: op_id → { model_key, est_bytes, generation, materialized, waiters }`
- `active_requests` is a **claim** count with a mandatory balanced release — see
  [contracts/admission-scheduler.md](./contracts/admission-scheduler.md) §Request claims.
- `waiters` is the registry of `AwaitLoad` joiners a single-flight load owns. Registration,
  deregistration, and claim assignment are all under the state lock, so the commit's count-then-assign is
  indivisible and a waiter that has given up cannot still be handed a claim. Every load-owning exit —
  commit, rollback, failure — disposes of its joiners; see §Load waiters.
- `exclusive_job: None | {job_id}`  ·  `generation` token per model_key
- `usable_capacity = min(configured_budget, NVML_total − safety_reserve)`
- **Five invariants** (assert-tested; authoritative text in
  [contracts/admission-scheduler.md](./contracts/admission-scheduler.md)):
  (1) `Σ residents.vram_accounted + Σ reservations ≤ usable_capacity`;
  (2) each incoming load `≤ live_free − unmaterialized − safety_headroom` (the v1 `Σ ≤ live_free`
  double-count is removed, and concurrent not-yet-visible loads are deducted);
  (3) no reservation is backed by a victim still resident;
  (4) `active_requests` equals the outstanding claim count and is never negative;
  (5) every registered `AwaitLoad` waiter reaches exactly one disposition, and no reservation is dropped
  leaving undisposed joiners behind.
- `vram_accounted_bytes` is reconciled from a **per-PID** NVML reading, not a device-wide delta — with
  concurrent loads a device-wide delta is not attributable to either operation.
- The lock guards **state only**, never held across load/unload (ABBA lesson). Empty during any exclusive job.
- Identity = model instance (shared across tenants), not tenant.

### Lease / claim
- `serving` claim: co-resident, two-bound-checked, evictable (idle/LRU) via **drain → wait active==0 → unload**.
- `exclusive` claim (job): requires empty serving set; whole GPU; never preempted.

### Queue (scheduler — persisted)
- `inference_lane`: admitted against the budget, interleaved; bounded by burst / job-drain mode.
- `jobs_lane`: FIFO of exclusive claims, **persisted in Postgres** (survives restart); owner override.
- Single GPU-ordering authority (host agent). `gateway/app/scheduler.py` is the 018 `PolicyScheduler`
  (drift/retrain monitoring), not a GPU-ordering authority — kept as-is; its retrains enter the jobs
  lane under a reserved system tenant.

## Relationships

```
tenant 1───* api_key
tenant 1───1 quota
tenant 1───* usage_ledger
tenant 1───* job
tenant 1───* session
job/inference/session ──emit──> usage_ledger (gpu_seconds)
ResidentServingSet / Queue ──runtime, referenced by──> admission + scheduler
```

## Requirement traceability

| Entity | Requirements |
|---|---|
| tenant, api_key | FR-002, FR-003, FR-004 |
| quota | FR-014, SC-005, SC-012 |
| usage_ledger | FR-007, FR-013, FR-015, FR-016, SC-004 |
| job | FR-008..013, FR-025, FR-026, SC-010, SC-011 |
| session | FR-020, FR-021, FR-022, SC-007 |
| ResidentServingSet / Lease / Queue | FR-006, FR-009, FR-010, FR-019, FR-023, FR-023a, FR-024, FR-025, SC-006 |
