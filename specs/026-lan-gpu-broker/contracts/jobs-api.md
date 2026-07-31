# Contract — Jobs API

Gateway routes, `Authorization: Bearer <tenant-key>` (TLS for multi-tenant, FR-002a). Jobs run with an
**exclusive** GPU lease, in a **hardened sandbox** — **GATED on a feasibility spike + runtime amendment**
(research R7); until that passes, only signed broker-owned recipes / native-Linux / deferral (not arbitrary
tenant code). Jobs are **never preempted** (FR-010). Quota is **reserved** at submit and **settled** to actual
GPU-seconds (= lease duration) on completion. The job queue is **persisted** (survives restart).

## POST /jobs  *(submit)* → FR-008, FR-026
Request (one of):
```jsonc
// batch
{ "kind": "batch", "image": "myimg:latest", "command": ["python","run.py"],
  "data_ref": "s3://.../set", "out_ref": "s3://.../results" }
// finetune (routes to the 010 trainer)
{ "kind": "finetune", "base": "qwen-0.5b", "modality": "vision",
  "data_ref": "s3://.../set", "params": { "epochs": 3 } }
```
Response `202`: `{ "job_id": "j-1a2b", "state": "queued", "queue_pos": 2, "lane": "jobs" }`.
Rejections: `400 invalid_spec` (malformed/oversized, before touching GPU); `403 quota_exhausted`.

## GET /jobs/{id}  *(status)* → FR-012
`{ id, state, queue_pos?, sandbox, gpu_seconds?, artifact_ref?, model_version? }`.

## GET /jobs/{id}/logs?follow=true  *(logs)* → FR-012
Streams stdout/stderr (SSE/chunked) while `running`.

## POST /jobs/{id}/cancel  *(cancel)*
`queued → cancelled` immediately; `running → cancelled` stops the sandbox and frees the GPU.

**Cancellation MUST resolve the job's `usage_reservation` in the same atomic step as the state change.**
GPU-seconds are reserved at *submission*, so a cancelled job never reaches the completion settlement
path — left alone, its reservation stays `reserved` forever and permanently withholds that much quota
from the tenant, refusing work they are entitled to run:

- `queued → cancelled`: mark the reservation `released` in full — no GPU time was consumed.
- `running → cancelled`: **settle elapsed** GPU-seconds to `usage_ledger` and release the remainder,
  charged against the reservation's stored `window_start` (see [data-model.md](../data-model.md)).

Both are idempotent under a repeated cancel, keyed by the job id.

## Guarantees
- A queued job acquires the lease automatically when the serving set can be drained; no manual step (FR-009).
- A finetune registers a model version with lineage + eval-gates (FR-011); batch produces artifacts.
- Isolation: no host filesystem mounts, non-root, restricted egress — verified by SC-011 (FR-026).
- Ledger entry (`kind=job`, gpu_seconds=lease duration) written on completion (FR-013).
