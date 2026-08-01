# Contract: Host-Agent Runtime API (027)

**Plane**: GPU host agent (`:8100`, stdlib transport) · **Callers**: the gateway **only**
(research R5) · **Auth**: `X-Agent-Key` required — these are not public probes (023 US2).

Three new routes plus one backward-compatible contract extension. All are **read-only**: nothing
here acquires, releases, or influences admission (Principle II).

---

## `GET /runtime/devices`

Per-device GPU snapshot (FR-375/381/382).

```json
{
  "observed_at": "2026-07-31T09:14:02Z",
  "source": "nvml",
  "devices": [
    {
      "index": 0, "name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU",
      "uuid": "GPU-1f2e…", "compute_capability": "12.0",
      "total_vram_gb": 12.0, "free_vram_gb": 7.4, "used_vram_gb": 4.6,
      "utilization_pct": 61, "temperature_c": 58,
      "processes": [{ "pid": 44121, "vram_gb": 4.6, "engine_id": "llm" }]
    }
  ]
}
```

**Rules**

- `devices` is **always a list**, even with one device — multi-device and multi-host (FR-382) need no
  contract change later.
- `source` ∈ `nvml` | `smi` | `static`. On `static`, per-device fields other than `index` MAY be
  `null`; the caller must **not** substitute zero. This is FR-381's fallback labelling as data.
- Served from the **existing 1-second-TTL cached reader** (research R2). This route MUST NOT fork a
  subprocess per request — that is the 018 regression NVML was introduced to remove.
- Side-effect free. It MAY take the admission lock for a consistent read; it MUST NOT claim, extend,
  or release.
- On an unreadable GPU the route returns `200` with `source: "static"` and nulls — **not** an error.
  An unreadable GPU is a known operating state, not a request failure.

---

## `GET /runtime/admission`

Recent admission decisions (FR-377/378).

```json
{
  "observed_at": "2026-07-31T09:14:02Z",
  "residents": [
    { "model_key": "job_3842", "kind": "job", "vram_gb": 10.1,
      "state": "resident", "active_requests": 1 }
  ],
  "usable_budget_gb": 11.4, "accounted_resident_gb": 10.1,
  "reserved_gb": 0.0, "unmaterialized_gb": 0.0, "live_free_gb": 1.6,
  "job_barrier": false, "capacity": 64,
  "records": [
    {
      "id": "adm_01J…", "op_id": "req-9f2", "tenant": "vision", "kind": "serving",
      "model_key": "clip-vit-b32", "requested_gb": 3.2, "attempt": 1,
      "decision": "refused", "reason": "job-exclusive",
      "residents": [
        { "model_key": "job_3842", "kind": "job", "vram_gb": 10.1,
          "state": "resident", "active_requests": 1 }
      ],
      "device_index": 0, "usable_budget_gb": 11.4, "accounted_resident_gb": 10.1,
      "reserved_gb": 0.0, "unmaterialized_gb": 0.0, "live_free_gb": 1.6,
      "explanation": "Refused: job job_3842 holds the GPU exclusively. A running job is never preempted.",
      "decided_at": "2026-07-31T09:13:58Z"
    }
  ]
}
```

**Rules**

- This is a **decision history for serving admission**, not a queue: a refused serving request will
  never later be granted, so there is no `pending` value and no queue-position field here, and the
  console must not present one (research R1). Bounded in-flight retries are reported as `attempt` on
  the record — a retry is not a queue place. The **jobs lane is a separate, genuine FIFO queue** with
  `queue_pos`, read from the gateway's `/admin/queue`, and is presented as one.
- The **two VRAM checks are reported separately**, each with **its own reservation term**:
  budget is `accounted_resident_gb + reserved_gb` against `usable_budget_gb`; live-VRAM is the
  incoming load plus headroom against `live_free_gb − unmaterialized_gb`. They MUST NOT be merged
  (`live_free_gb` already excludes residents, so summing the resident set against it double-counts —
  the v1.6.0 defect v1.6.1 corrected), and neither reservation term may be dropped: `reserved_gb`
  counts every outstanding reservation, `unmaterialized_gb` only those not yet reconciled to a real
  delta, because a materialized one is already reflected in `live_free_gb`.
- `residents[]` is keyed by **`model_key`, not tenant** — the coordinator shares one resident child
  across every tenant requesting that model. Each carries its lifecycle `state` and `active_requests`
  so a mid-transition read is not mistaken for a violated invariant.
- Backed by a **bounded in-memory ring** (default 64) written by the coordinator as each decision
  returns. Bounded because it must not grow unboundedly in a long-lived agent, and in-memory because
  a decision history is diagnostic, not durable — losing it on restart is acceptable, and persisting
  it would mean a migration for no operational gain.
- `explanation` is composed **server-side** from the same values the decision used, so the interface
  cannot drift from admission's real reasoning. Templates are fixed in
  [data-model.md §6](../data-model.md).
- Recording MUST NOT extend the critical section: the ring append happens after the decision is
  determined, inside the existing lock scope, and MUST NOT perform IO.

---

## `GET /journal`

Paged durable-journal read (FR-380).

**Query**: `cursor` (opaque, sequence-based) · `limit` (default 100, **hard cap 500**) ·
`job_id` · `engine_id` · `event_type` · `since` / `until`.

```json
{
  "entries": [
    {
      "sequence": 91442, "timestamp": "2026-07-31T09:02:11Z",
      "event_type": "transition", "job_id": "job_3842", "engine_id": null,
      "pid": 44980, "device_index": 0,
      "from_state": "admitted", "to_state": "running",
      "detail": null, "checksum_state": "ok"
    }
  ],
  "next_cursor": "seq:91343",
  "has_more": true
}
```

**Rules**

- Newest-first. Paging is **mandatory** — there is no "all" mode. The journal grows without bound
  across a machine's life, and the agent transport's 1 MiB JSON cap (023 US6) would fail an
  unbounded response anyway, so paging is a correctness requirement, not a nicety.
- `checksum_state` surfaces the 019 torn-tail handling honestly: a `torn` tail entry is **shown as
  torn**, never silently dropped, because a missing final transition is exactly what an operator
  investigating a crash needs to see.
- Read-only. This route never triggers replay or `mark_interrupted`.

---

## Extension: `EngineState` (backward-compatible)

`platformlib/contracts.py::EngineState` gains **optional fields only** (research R3):

| Field | Type | Notes |
|---|---|---|
| `pid` | `int?` | child process |
| `device_index` | `int?` | null for CPU modalities |
| `vram_gb` | `float?` | null when not GPU-resident |
| `model_identity` | `str?` | **agent-reported loaded identity** (022) — never the desired pointer |
| `registry_version` | `str?` | version actually loaded |
| `started_at` | `str?` | ISO-8601 |
| `active_requests` | `int?` | in-flight count |

**Compatibility requirement**: every field is `Optional` with a default, so the 018/019 `/health` and
`/engines` conformance tests continue to pass **unchanged**. A consumer written before 027 sees the
same payload shape it always did. This is the reason for extending `EngineState` rather than
introducing a parallel engine contract that would fork the validated shape both runtimes exchange.

**Honesty requirement**: `model_identity` and `registry_version` MUST be sourced from what the
adapter actually loaded. Populating them from the registry's desired pointer would manufacture the
precise class of falsehood FR-427 exists to prevent — and would silently defeat 022's honest
served-identity work.
