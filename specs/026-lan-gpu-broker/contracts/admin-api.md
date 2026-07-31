# Contract — Admin API (owner-only)

Owner-scoped routes (served via the console BFF / a privileged key). Manage tenants, keys, quotas, and
observe the queue/usage. → FR-004, FR-014, FR-017.

## POST /admin/tenants  *(create tenant + first key)*
`{ "name": "alice" }` → `201 { "tenant_id", "api_key": "sk-…" }`  *(raw key shown once)*.

## POST /admin/tenants/{id}/keys  *(rotate/add key)* → `201 { "api_key" }`
## POST /admin/tenants/{id}/revoke  *(disable tenant)* → `200` — all keys denied, in-flight work ends gracefully.
## POST /admin/keys/{id}/revoke  *(revoke one key)* → `200`.

## PUT /admin/tenants/{id}/quota  *(set quota)* → FR-014
`{ "window": "daily", "budget_gpu_seconds": 3600 }` → `200`.

## GET /admin/usage?tenant=&window=  *(usage)* → FR-017, SC-004
`{ per_tenant: [{ tenant, consumed_gpu_seconds, remaining, window_start }], total }`.

## GET /admin/queue  *(live queue + residency)* → FR-017, SC-006, SC-009
```jsonc
{ "resident": [{ "modality":"chat", "model":"qwen", "vram_mb":5400, "idle":false,
                 "state":"resident", "active_requests":2 }],
  "reservations": [{ "op_id":"req-9f2", "model":"whisper", "est_mb":1200,
                     "materialized":false }],
  "vram": { "usable_capacity_mb":  11264,  // min(configured_budget, total − safety_reserve)
            "accounted_mb":         5400,  // Σ resident.vram_accounted — invariant 1, left side
            "reserved_mb":          1200,  // Σ ALL reservations.est_mb — invariant 1, left side
            "unmaterialized_mb":    1200,  // Σ reservations NOT yet reconciled — invariant 2 only
            "live_free_mb":         5100,  // NVML, instantaneous
            "safety_headroom_mb":    512 },
  "inference_lane": [ /* pending inference */ ],
  "jobs_lane": [ { "job_id":"j-1a2b", "tenant":"bob", "pos":1 } ],
  "active_job": null,
  "job_barrier": false }
```

**Why the VRAM block is shaped this way.** [quickstart.md](../quickstart.md) Drill 3 asks an operator to
assert invariant 1 — `accounted + reserved ≤ usable_capacity` — and invariant 2 per load. An earlier
revision exposed only `vram_free_mb` and per-resident `vram_mb`, so neither bound was checkable from the
documented surface: outstanding reservations were invisible, and `usable_capacity` (which is *not* the
device total) was nowhere. Both terms of each bound are now first-class fields, so the drill reads values
rather than inferring them. `state` and `job_barrier` are included for the same reason — a `loading` or
`draining` resident is otherwise indistinguishable from a settled one, which is what makes a
mid-transition observation look like a violated invariant.

**`reserved_mb` and `unmaterialized_mb` are different sums and are both needed.** The two invariants do
not take the same reservation term: invariant 1 counts **every** outstanding reservation against the
budget, while invariant 2 deducts only those **not yet reconciled to a real delta** from live-free —
because a reservation that has already materialized is by then visible in `live_free` itself, and
subtracting it twice would refuse valid loads. `unmaterialized_mb` is derivable by summing the
`reservations` entries with `materialized: false`, but an operator mid-drill should not have to do that
arithmetic to check a bound, and getting it wrong silently produces a *passing* assertion against the
wrong quantity.

Field names follow [admission-scheduler.md](./admission-scheduler.md) wherever it names a quantity —
`usable_capacity`, `accounted`, `unmaterialized`, `live_free`, `safety_headroom` are all its terms.
`reserved_mb` is the one exception: that contract only ever writes it inline as `Σ reservations.est_bytes`
and never names it, so this endpoint names it here.

## POST /admin/jobs/{id}/{pin|pause|reorder}  *(owner override)* → FR-025
Adjust jobs-lane ordering. A running job is never preempted by these controls.
