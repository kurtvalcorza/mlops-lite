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
| `est_gpu_seconds` | numeric | estimated/max charge held against quota before work |
| `state` | enum(`reserved`,`settled`,`released`) | settled → a `usage_ledger` row written; released → unused |
| `created_at` / `settled_at` | timestamptz | |

**Rule**: reserve (atomic, rejects if it would exceed the window budget) → do work → settle actual to
`usage_ledger` + release remainder. Durable settlement via outbox/WAL if the store is briefly unavailable.

### job
| Field | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK→tenant | |
| `kind` | enum(`batch`,`finetune`,`hpo`) | |
| `spec` | jsonb | image/entrypoint or finetune spec (base, data ref, params) |
| `state` | enum(`queued`,`running`,`succeeded`,`failed`,`cancelled`) | |
| `queue_pos` | int null | while queued |
| `sandbox` | text | resolved sandbox runtime (kata/runsc/fallback) |
| `artifact_ref` / `model_version` | text null | outputs; finetune → MLflow version |
| `gpu_seconds` | numeric null | filled on completion (= lease duration) |
| `created_at`/`started_at`/`ended_at` | timestamptz | |

**State transitions**: `queued → running → (succeeded|failed)`; `queued → cancelled`;
`running → cancelled` (tenant/owner). `running` is never forced to `queued` (no preemption).

### session
| Field | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK→tenant | |
| `state` | enum(`active`,`idle`,`released`,`expired`) | |
| `idle_timeout_s` / `ttl_s` | int | guard rails |
| `last_activity_at` | timestamptz | drives idle-cull |
| `created_at`/`ended_at` | timestamptz | |

**Transitions**: `active ⇄ idle`; `idle →(idle_timeout)→ released`; `active/idle →(ttl)→ expired`.

## Runtime state (host agent, in-process — not persisted)

### GPU coordinator state  *(state machine — corrected per Codex review)*
- `residents: model_key → { state: loading|resident|draining|evicting, vram_accounted_bytes, active_requests, last_used_at }`
- `reservations: op_id → { model_key, est_bytes, generation }`
- `exclusive_job: None | {job_id}`  ·  `generation` token per model_key
- `usable_capacity = min(configured_budget, NVML_total − safety_reserve)`
- **Two invariants** (assert-tested): `Σ residents.vram_accounted + Σ reservations ≤ usable_capacity` **and**
  each incoming load `≤ live_free − safety_headroom` (the v1 `Σ ≤ live_free` double-count is removed).
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
