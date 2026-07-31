# Quickstart — LAN GPU Broker validation drills

On-hardware validation that the feature works end-to-end. Each drill maps to a user story + success
criteria. Endpoints per [contracts/](./contracts/); entities per [data-model.md](./data-model.md). Run on the
target WSL GPU host with the stack up (`nvidia-smi` must succeed — Gate Zero).

**Except Drill 2b**, which is gated on a native-Linux GPU host and MUST NOT be run or waived on WSL2 —
the pass criterion below is "every ungated drill passes", not "every drill passes".

**Prereqs**: stack running; gateway bound to the LAN interface (research R8); at least one small serving model
in the local zoo; a second LAN device (or a second shell on another host) for concurrency drills.

---

## Drill 1 — Self-service inference (US1 · SC-001/002/003)
1. Owner: `broker admin tenant create --name alice` → capture key.
2. From a **LAN device**, call `/v1/chat/completions` with the key → expect a completion + `X-GPU-Seconds`.
3. From **two devices at once**, fire N chat requests → all succeed, none dropped (SC-002).
4. Call with a bogus key → `401` and **no** GPU work (SC-003).
5. `curl` the gateway from **off-LAN** → unreachable (SC-008).

## Drill 2 — Jobs: queue and exclusivity (US2 · SC-006/010)
1. Tenant A: `broker submit … -- python train.py` → `202 queued`.
2. Tenant B: submit a second job → `queued, pos 2`.
3. Watch `/admin/queue`: job A `running` (exclusive), B waits; **no** serving tenant resident during the job.
4. While A runs, submit a **third** job → it queues; it must **not** displace A's exclusive claim.
5. While A runs, send an inference request → `503 gpu_busy` with `Retry-After` (or queued) — job **not**
   preempted (SC-010).
6. On A finish, B starts automatically (FR-009); finetune → registered model version appears in MLflow.
7. **Drain convergence**: with a model *actively serving* (keep a stream of requests in flight), submit a
   job → the job acquires the GPU once those in-flight requests finish. It must **not** sit until
   `job_drain_timeout`; a busy resident is drained, not skipped.
8. **In-flight load vs. the barrier**: start a cold-model request (one that must load) and submit a job
   immediately after, so the load is in flight when the barrier rises → the load rolls back, the request
   gets a retryable `503 gpu_busy`, the job starts with an empty serving set, and the retried request
   succeeds once the job ends. No model is ever resident alongside the job.

## Drill 2b — Job sandbox isolation (US2 · SC-011) — **NATIVE-LINUX HOST ONLY, GATED**
> **Do not run this drill on the WSL2 host, and do not substitute weaker isolation to make it pass.**
> The completed feasibility spike ([spikes/](./spikes/)) found gVisor/Kata GPU isolation **infeasible on
> WSL2** — the GPU is paravirtualized through `/dev/dxg`, with no `/dev/nvidia*` and no PCI GPU to pass
> through. SC-011 and FR-026 are therefore gated on the P2 migration to a native-Linux GPU host. Running
> arbitrary tenant code without the hardened sandbox is exactly what this gate exists to prevent.

Once P2 is unblocked on native Linux:
1. Isolation: submit a job that tries to read a host path / reach host services / open external egress →
   blocked by the sandbox, no host effect (SC-011).

## Drill 3 — Co-residency & eviction (US4 · SC-006)
1. With budget headroom, load a small LLM (chat request), then send an **ASR** request whose model also fits
   → both become **co-resident**; `/admin/queue` shows two resident (SC-006).
2. Request a **large** model that doesn't fit → admission evicts idle/LRU serving tenants; if still too big →
   `413 model_too_large`. If the refusal is caused by another tenant's in-flight reservation or an external
   GPU consumer rather than the model's own size, expect a **retryable `gpu_busy`**, not `413`.
3. Assert (via `/admin/queue`'s `vram` block, or agent logs/metrics) the **two bounds separately** — the
   single `Σ resident.vram ≤ live_free_vram`
   assertion this drill used to make was the rejected v1 condition and fails on valid states (live free
   already excludes the residents, so any model over half the device trips it):
   - accounted set: at no instant `Σ residents.vram_accounted + Σ reservations > usable_capacity`;
   - per load: every recorded load passed its *contemporaneous* `live_free − unmaterialized −
     safety_headroom` check at the moment it was admitted.
4. **Coalescing and waiter disposal**: fire N simultaneous first-requests for one cold model → exactly one
   load happens and all N are served. Repeat with the load forced to fail (point at a missing model) →
   every one of the N is refused promptly, none waits out its deadline, and the model does not stay
   `loading`. Then repeat against a near-full GPU so the load rolls back on drift → the joined waiters are
   likewise answered inside the loader's own timeframe, and `active_requests` settles to 0 with the model
   evictable afterwards (invariants 4 and 5).

## Drill 4 — Quotas & ledger (US3 · SC-004/005/012)
1. Owner: `broker admin quota set --tenant alice --window daily --gpu-seconds 30`.
2. Alice runs work past 30 GPU-s → next request `403 quota_exhausted` (SC-005); a **different** tenant still
   succeeds.
3. `GET /admin/usage` → ledger totals reconcile with work done within 5% (SC-004).
4. Simulate window rollover (advance window / test hook) → Alice's budget auto-resets, work resumes (SC-012).

## Drill 5 — Interactive session guard rails (US5 · SC-007) — **GATED on T665**
> The session **admission class** is undecided (exclusive · sandboxed job · distinct class) and gates all
> of P5 — see [research.md](./research.md) R9. Run this drill only after T665 closes; how a session
> acquires the GPU in step 1 is exactly what that decision determines.

1. `broker session start --ttl 2h --idle 5m` → notebook URL.
2. Leave idle > 5 min **with the notebook client open and heartbeating normally** (do not close the tab —
   an automatic liveness heartbeat must not hold the GPU) → GPU lease auto-released (`state=released`);
   `/admin/queue` shows GPU freed (SC-007).
3. Start a session, submit a finetune **as a job** from a cell → GPU held only for the run, not the session.

## Drill 6 — Owner visibility (US3 · SC-009)
Open the console → confirm live queue depth, resident tenants + VRAM headroom, and per-tenant usage vs quota.

---

**Pass criteria**: every **ungated** drill green on the target hardware (Drill 2b is native-Linux-only and
is *not* waivable on WSL2 — see above); the five coordinator invariants hold throughout — accounted set +
reservations ≤ `usable_capacity`, each load within `live_free − unmaterialized − safety_headroom`, no
reservation backed by a still-resident victim, `active_requests` balanced, and every `AwaitLoad` waiter
disposed of exactly once — plus an empty serving set
during any job; zero job preemptions; 100% of unauthorized/over-quota requests refused; broker unreachable
off-LAN.
