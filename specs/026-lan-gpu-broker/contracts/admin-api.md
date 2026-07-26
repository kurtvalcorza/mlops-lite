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
{ "resident": [{ "modality":"chat", "model":"qwen", "vram_mb":5400, "idle":false }],
  "vram_free_mb": 9000,
  "inference_lane": [ /* pending inference */ ],
  "jobs_lane": [ { "job_id":"j-1a2b", "tenant":"bob", "pos":1 } ],
  "active_job": null }
```

## POST /admin/jobs/{id}/{pin|pause|reorder}  *(owner override)* → FR-025
Adjust jobs-lane ordering. A running job is never preempted by these controls.
