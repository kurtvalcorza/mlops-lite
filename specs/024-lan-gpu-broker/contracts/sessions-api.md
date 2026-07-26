# Contract — Interactive Sessions API

Gateway routes, `Authorization: Bearer <tenant-key>`. A session is a serving-class lease with idle-cull +
TTL guard rails (research R9). → FR-020, FR-021, FR-022.

## POST /sessions  *(start)* → FR-020
`{ "idle_timeout_s": 600, "ttl_s": 7200 }` → `201 { "session_id", "notebook_url", "expires_at" }`.
Subject to owner policy (may be disabled per tenant).

## GET /sessions/{id} → `{ id, state, last_activity_at, expires_at }`  (state: active|idle|released|expired)

## POST /sessions/{id}/heartbeat  *(activity ping)*
Resets the idle timer; GPU work also resets it implicitly.

## DELETE /sessions/{id}  *(end)* → releases the GPU immediately.

## Guard rails (enforced by the host agent)
- **Idle-cull**: no GPU activity for `idle_timeout_s` → GPU lease released, `state=released` (FR-021, SC-007).
- **TTL**: `ttl_s` elapsed → `state=expired`, GPU freed (FR-021).
- Under contention a session is the **lowest-priority** serving tenant to keep resident (R9).

## Notebook-as-job helper → FR-022
Training from a session SHOULD use the jobs path, not the kernel. Client helper
`broker.finetune(...)` / `%%gpu` submits a job (see jobs-api) and streams logs into the notebook, so the GPU
is held only for the run — not the whole session.
