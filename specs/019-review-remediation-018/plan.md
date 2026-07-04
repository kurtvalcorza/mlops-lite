# Implementation Plan: 018 Review Remediation

**Branch**: `019-review-remediation-018` | **Date**: 2026-07-03 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/019-review-remediation-018/spec.md`. Sourced from a
high-effort, recall-biased code review of the 018 fold-in (`master..HEAD`): 8 independent finder angles →
per-candidate verification. Ten findings survived; the four P1s are CONFIRMED live correctness bugs.

## Summary

Fix the ten verified defects the 018 platform re-architecture introduced, without adding any capability,
service, runtime, or dependency. The four P1s are live correctness bugs: a journal that silently drops a
durable transition after a torn-tail crash (US1), a GPU lease that permanently wedges other tenants after a
dropped release (US2), a swap probe that evicts the resident holder for a target that can't serve (US3),
and a non-idempotent retrain launch that double-runs on ordinary network/store faults (US4). Two P2
concurrency fixes tighten single-tenant admission (US5 lifecycle lock discipline, US6 single-flight swap
reservation). One P2 aligns the agent's `/health` and `/engines` producers to the existing
`platformlib.contracts` shapes (US7). Two P3s restore per-engine startup/idle tuning (US8) and make
shadow-verdict selection total (US9). Every fix is guarded by a regression test that fails on the current
code and passes after; the existing suite stays green (SC-126).

## Technical Context

**Language/Version**: Python 3.12 (host agent, `hostagent/`), Python 3.11+ (gateway container). No UI
change. No new runtime.

**Primary Dependencies**: None added. Fixes touch existing modules only (`hostagent/journal.py`,
`hostagent/lifecycle.py`, `hostagent/admission.py`, `hostagent/adapters/__init__.py`, `hostagent/main.py`,
`serving/gpu_lease.py`, `gateway/app/swap.py`, `gateway/app/scheduler.py`, `platformlib/contracts.py`
consumers). Principle: `platformlib` stays stdlib-only (unchanged).

**Storage**: Unchanged. The journal (JSONL append log) and lease (lockfile) formats are preserved; US1 only
tightens the append/replay procedure and directory-fsync discipline, US2 only changes reclaim behavior on a
same-owner tenant switch.

**Testing**: `pytest`. Each fix ships a regression test that reproduces the finding on the pre-fix code.
GPU-hardware-only behaviors (real swap eviction on the RTX box) are covered by fakes/mocks in CI plus the
existing on-hardware runbook where a live GPU is required; the fixes themselves are exercised with
fault-injecting fakes (torn file, failing store, 503 probe, interleaved load/unload, missing `LastModified`).

**Target Platform**: Win11 + WSL2 + NVIDIA, unchanged. No endpoint/URL changes; the agent's HTTP surface is
unchanged except US7 flattens the `/health` GPU fields and adds `engine_id` to `/engines` rows to match the
contracts (both currently have no consumer, so this is backward-safe).

**Project Type**: Remediation within existing packages. No new package, no new service.

**Performance Goals**: No regression; a few fixes *reduce* work (e.g. US7 avoids a double `engine_states()`
sweep if folded in). Swap/inference latency unchanged.

**Constraints**: Principle II at every instant — US5/US6 strengthen it; no fix may open a two-model window
or preempt a running training/HPO/batch job. External API byte-compatible on the happy path. Default
(non-preempt, single-tenant, manual-promotion) behavior preserved.

**Scale/Scope**: 10 defects, ~11 changed source files, ~9 new regression tests. IDs continue the shared
space: FR-188..197, SC-117..126, tasks T382..T400.

## Constitution Check

*GATE: must pass before implementation.* Constitution **v1.4.1**.

| Principle | Assessment |
|---|---|
| **I. Local-First** | ✅ No cloud; all fixes are intra-host. Offline behavior unchanged. |
| **II. Single-GPU On-Demand (NON-NEGOTIABLE)** | ✅ **Strengthened, never weakened.** US5 removes an unlocked read that could release admission mid-load (a latent two-model window); US6 makes the swap reservation single-flight per target so the evict→re-acquire window stays closed; US3 stops an evict-then-fail that could leave zero models serving; US2 restores lease self-recovery without loosening cross-process exclusion (a *different* live PID is still refused). No fix admits a second tenant or preempts a running job. |
| **III. Lightweight Footprint** | ✅ Zero new dependency, service, or runtime. Fixes are localized edits + tests. |
| **IV. Reproducible / Closed-Loop** | ✅ US4 makes the retrain loop *more* reliable (exactly-once launch per breach); US9 makes promotion-verdict selection deterministic. No loosening. |
| **V. Contract-Tested** | ✅ US7 brings the `/health` and `/engines` producers into conformance with the already-committed `AgentHealth`/`EngineState` contracts — closing a producer/contract drift rather than changing a contract. |

**No constitution amendment required** — 019 fixes defects against the existing v1.4.1 rules; it introduces
no new behavior a principle would need to describe.

## Approach per fix

- **US1 (journal)**: on `_append`, before writing, ensure the file ends on a clean record boundary — seek to
  EOF, and if the last byte is not `\n`, either truncate the torn partial line or write a leading `\n` so the
  new record starts its own line; fsync the containing directory on first create. Order `transition()` so the
  in-memory mutation follows a successful durable append (or is rolled back on failure).
- **US2 (lease)**: in `acquire()`, when the record is ours (live PID + start-time) but a different tenant,
  reclaim it (self-heal the same-owner record) instead of raising `LeaseHeld`; keep raising for a *different*
  live owner. Surface/log a swallowed `release()` failure in `admission.release()`.
- **US3 (swap probe)**: `_default_target_probe` must inspect the response status — reachable only on 2xx (or
  an explicit readiness signal); a non-2xx refuses the swap before eviction, and the refusal is
  distinguishable from "holder busy".
- **US4 (scheduler)**: attach a stable per-breach run key to `/train`, and mark the park **launched before**
  issuing the request so a lost response is reconciled (not read as failure); on the double store-write
  failure, ensure the next tick treats an already-launched breach as landed. A genuinely-failed launch (no
  run created) remains retriable.
- **US5 (lifecycle)**: read/write `_loading` and the teardown decision under `self.lock`; the failed-acquire
  hard-cut path must not read the guard or release admission outside the lock.
- **US6 (admission)**: make the swap reservation single-flight per target (reject-or-wait a concurrent
  same-target `begin_swap`) and refcount-safe so `end_swap` never drops a reservation another swap holds.
- **US7 (contracts)**: flatten the `/health` GPU fields to `gpu_free_gb`/`holder`/`holder_kind`/`wedged`;
  include `engine_id` in each `/engines` row. Add contract round-trip tests as the guard.
- **US8 (budgets)**: thread a per-engine `ready_wait_s` (and per-engine idle) from the registry/env into
  `build_runtimes`, restoring the slow-bento startup grace.
- **US9 (verdict)**: make newest-verdict selection total — use a secondary key (e.g. object key / uuid) when
  `LastModified` is absent, or fail-closed rather than silently pick a stale verdict.

## Phasing

P1 fixes (US1–US4) land first and independently — each is a standalone MVP slice with its own regression
test. P2 (US5–US7) and P3 (US8–US9) follow. Every merged step keeps the full suite green (SC-126).
