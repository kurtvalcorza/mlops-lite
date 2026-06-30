# Contract: `unload-now` control endpoint (per serving supervisor) — FR-156

A new control endpoint on each **preemptable serving supervisor** (llama, whisper.cpp, BentoML vision).
Called **only by the gateway** during a swap.

## `POST /unload-now`

Drain in-flight work, unload the model, release the GPU lease.

### Request

```json
{ "drain_timeout_s": 10 }
```

### Behavior

| Step | |
|---|---|
| 1. drain | finish current in-flight request(s); wait up to `drain_timeout_s` |
| 2. unload | reuse the supervisor's existing `_unload` (terminate the model proc, free VRAM) |
| 3. release | `gpu_lease.release(<tenant>)` — drop the 008 lease |
| timeout | if drain exceeds `drain_timeout_s` → hard unload (kill now) |

### Responses

| Case | Response |
|---|---|
| Unloaded + released | `200 { "status": "unloaded", "drained": true|false }` |
| Not resident (nothing to unload) | `200 { "status": "idle" }` (no-op; lease already free) |

### Invariants & guards

- **Authenticated** like other control routes (it is destructive) — *(auth scheme pinned in tasks)*.
- Idempotent: a second `unload-now` after release is a no-op (`idle`).
- The supervisor releases the lease **only** for its own tenant (never another's).
- **Serving supervisors only** — training/HPO/batch daemons do **not** expose `unload-now` (FR-155: not
  preemptable).
