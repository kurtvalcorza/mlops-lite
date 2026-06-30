# Phase 1 Data Model: Swap-on-Demand (017)

A **control-plane** feature — no persistent data. Three transient control entities + the reused lease.

## Entity: Preempt request

A serving request carrying the explicit opt-in to swap.

| Field | Notes |
|---|---|
| `preempt` | bool, **default false**; true authorizes a swap if a *serving* model is resident (FR-154) |
| (request body) | otherwise the normal serving request (prompt / image / audio) |

- Default false ⇒ 008 refuse-if-held, unchanged (SC-101).

## Entity: Unload-now command (gateway → holder)

A control call from the gateway to the current holder's supervisor.

| Step | Behavior |
|---|---|
| drain | finish in-flight request(s) up to a bounded **drain timeout** (FR-156) |
| unload | reuse the supervisor's existing `_unload` (kill model proc, free VRAM) |
| release | drop the 008 lease (`gpu_lease.release(tenant)`) |
| past timeout | hard unload (don't hang the swap) |

- Only valid against a **serving** holder; never sent to a training/HPO/batch holder (US2/FR-155).

## Entity: Swap (sequential, lease-arbitrated)

```
gateway receives serving request with preempt=true
  → holder = current_holder()  (via /serving/state)
  → if holder is TRAINING/HPO/BATCH  → REFUSE (FR-155, never evict work)
  → if holder is a SERVING tenant    → POST unload-now to holder
       → wait until the lease is free (current_holder() == None / changed)   # FR-157
       → forward the request to the TARGET serving daemon (acquire + load)   # one model in VRAM
  → if no holder                     → normal serve (no swap needed)
```

- **Invariant**: at most one model resident at any instant (Principle II / FR-158) — evict → free → load
  is strictly sequential; concurrent `preempt=true` requests serialize on the lease.
- **Holder kind** (serving vs training) is read from the lease/`/serving/state` `tenant` — the gateway needs
  to distinguish a serving holder (preemptable) from a training holder (not).
- **Failure**: target fails to load after eviction → surface the load error (FR-159); holder dies mid-swap →
  008's stale-reclaim frees it (no deadlock).

## Reused (unchanged)

- **`serving/gpu_lease.py`** — `current_holder()`, `release(tenant)`, the stale-reclaim. No primitive
  change; 017 drives it via the gateway + `unload-now`.

See [contracts/unload-now-endpoint.md](contracts/unload-now-endpoint.md) and
[contracts/preempt-flag.md](contracts/preempt-flag.md).
