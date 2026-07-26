# Quickstart — LAN GPU Broker validation drills

On-hardware validation that the feature works end-to-end. Each drill maps to a user story + success
criteria. Endpoints per [contracts/](./contracts/); entities per [data-model.md](./data-model.md). Run on the
target WSL GPU host with the stack up (`nvidia-smi` must succeed — Gate Zero).

**Prereqs**: stack running; gateway bound to the LAN interface (research R8); at least one small serving model
in the local zoo; a second LAN device (or a second shell on another host) for concurrency drills.

---

## Drill 1 — Self-service inference (US1 · SC-001/002/003)
1. Owner: `broker admin tenant create --name alice` → capture key.
2. From a **LAN device**, call `/v1/chat/completions` with the key → expect a completion + `X-GPU-Seconds`.
3. From **two devices at once**, fire N chat requests → all succeed, none dropped (SC-002).
4. Call with a bogus key → `401` and **no** GPU work (SC-003).
5. `curl` the gateway from **off-LAN** → unreachable (SC-008).

## Drill 2 — Jobs: queue, exclusivity, sandbox (US2 · SC-006/010/011)
1. Tenant A: `broker submit … -- python train.py` → `202 queued`.
2. Tenant B: submit a second job → `queued, pos 2`.
3. Watch `/admin/queue`: job A `running` (exclusive), B waits; **no** serving tenant resident during the job.
4. While A runs, send an inference request → `409 gpu_busy` (or queued) — job **not** preempted (SC-010).
5. On A finish, B starts automatically (FR-009); finetune → registered model version appears in MLflow.
6. Isolation: submit a job that tries to read `/etc/hostname`-of-host / a host path / external egress →
   blocked by the sandbox, no host effect (SC-011).

## Drill 3 — Co-residency & eviction (US4 · SC-006)
1. With budget headroom, load a small LLM (chat request), then send an **ASR** request whose model also fits
   → both become **co-resident**; `/admin/queue` shows two resident, `Σvram ≤ free` (SC-006).
2. Request a **large** model that doesn't fit → admission evicts idle/LRU serving tenants; if still too big →
   `413 model_too_large`.
3. Assert (agent logs/metrics): at no instant `Σ resident.vram > live_free_vram`.

## Drill 4 — Quotas & ledger (US3 · SC-004/005/012)
1. Owner: `broker admin quota set --tenant alice --window daily --gpu-seconds 30`.
2. Alice runs work past 30 GPU-s → next request `403 quota_exhausted` (SC-005); a **different** tenant still
   succeeds.
3. `GET /admin/usage` → ledger totals reconcile with work done within 5% (SC-004).
4. Simulate window rollover (advance window / test hook) → Alice's budget auto-resets, work resumes (SC-012).

## Drill 5 — Interactive session guard rails (US5 · SC-007)
1. `broker session start --ttl 2h --idle 5m` → notebook URL.
2. Leave idle > 5 min → GPU lease auto-released (`state=released`); `/admin/queue` shows GPU freed (SC-007).
3. Start a session, submit a finetune **as a job** from a cell → GPU held only for the run, not the session.

## Drill 6 — Owner visibility (US3 · SC-009)
Open the console → confirm live queue depth, resident tenants + VRAM headroom, and per-tenant usage vs quota.

---

**Pass criteria**: all drills green on the target hardware; the admission invariant
(`Σ resident.vram ≤ live_free_vram`, empty serving set during a job) holds throughout; zero job preemptions;
100% of unauthorized/over-quota requests refused; broker unreachable off-LAN.
