---
description: "Task list for Race-Free GPU Lease & Vision-on-GPU (008)"
---

# Tasks: Race-Free GPU Lease & Vision-on-GPU

**Input**: Design documents from `specs/008-gpu-lease/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened, refreshed platform
(002/004/005/006/007). Generalizes the one-model-in-VRAM mutex into a race-free GPU lease; moves vision
onto the GPU; makes the Infer tab GPU-aware. **Carries a constitution amendment (Principle II → v1.4.0).**

**Tests**: Re-run the full 001/006 integration suite (serving, training, tracing, SSE framing,
idle-release, six UI tabs + BFF) per phase on the target GPU before the next. Task IDs continue the shared
space (T132+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **DRAFT — GRILLED (2026-06-28), build-ready.**
> Scope: **race-free GPU lease** (atomic admission + live-VRAM check, subsumes both current guards) +
> **vision-on-GPU** under the lease + **GPU-aware Infer tab**. Behavior-preserving for LLM/training. GPU
> stack **frozen** (007 FR-060); **no new dependency**. **Constitution amendment: YES — Principle II
> v1.3.0→v1.4.0** (generalize to a single race-free GPU lease; ratified at build time by T132, live
> constitution.md not edited until then, as 003 did for v1.3.0). Tasks T132–T153.
>
> **Decided (firm FRs):**
> 1. One **atomic, single-slot, self-healing** GPU lease; exactly one tenant resident at any instant;
>    subsumes `trainer.py` refuse-while-serving (409) + `supervisor.py` refuse-while-training. (FR-062/063)
> 2. Admission checks **live `nvidia-smi` free VRAM**, replacing the static `_fits()` estimate. (FR-064)
> 3. Lease is **modality-general**; **vision** moves onto the GPU as a tenant (reusing cu128 torchvision,
>    no new dep); ASR serving deferred to **009**. (FR-065/066)
> 4. Infer tab: **remove** the misleading stream `model:` dropdown → **read-only** `serving: <model>@vN ·
>    resident|idle` status line; `classify` becomes GPU-state-aware. (FR-069/070)
>
> **Grilled decisions (2026-06-28):**
> 1. **Lease primitive = ATOMIC LOCKFILE.** A PID-stamped lockfile on the WSL Ubuntu filesystem
>    (`O_CREAT|O_EXCL` atomic create) with `os.kill(pid, 0)` stale-holder reclamation, acquired by the
>    **three native WSL daemons** (serving supervisor, trainer, bento/vision) via a shared stdlib
>    `serving/gpu_lease.py` module — they self-arbitrate. *Why*: the GPU is held by native WSL daemons, the
>    Docker gateway only proxies + never holds the GPU and training is fire-and-forget, so a gateway-brokered
>    token would couple admission to the gateway + make it stateful (lease lost on restart) and an in-gateway
>    asyncio semaphore can't gate long-running native work across separate OS processes. Stdlib-only (no dep,
>    Principle III); the gateway only **reads** the holder via `/health` for the UI (FR-068), never
>    arbitrates. (FR-062/063)
> 2. **Classify-on-busy = A1 (DISABLE WITH HINT).** When another tenant holds the lease, the Infer
>    `classify` control is disabled with a hint ("GPU busy: LLM resident"); the operator frees the GPU
>    (idle-release or stop serving) to classify — cooperative refuse-if-held, NO preemption built. **A2
>    swap-on-demand** is DEFERRED as a documented fast-follow (preemptive evict/reload, out of 008 scope).
>    (FR-070)

---

## Phase 0 — Ratify amendment + pre-flight (gates everything)

- [ ] **T132** Ratify the **v1.4.0 amendment** to `constitution.md`: rewrite **Principle II** to the
  generalized "single race-free GPU lease over exactly one tenant (any GPU-resident modality OR a training
  run); CPU-only models exempt; live-VRAM admission; on-demand load + idle-release + VRAM budget retained;
  NON-NEGOTIABLE" wording (verbatim text in plan.md → *Constitution Amendment (v1.4.0)*); bump `Version`
  1.3.0→1.4.0, set `Last Amended: 2026-06-28`, append the v1.4.0 changelog note. *(Build-time ratify, as
  003 did for v1.3.0 — this is the ONLY edit to the live constitution in 008.)*
- [ ] **T133** Pre-flight: confirm `nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits` reads
  cleanly on the WSL GPU host (the read `trainer.py` already uses); confirm `torchvision==0.26.0+cu128` is
  importable in the native env for vision-on-GPU. (Grill resolved 2026-06-28: lease primitive = **atomic
  lockfile**; classify-on-busy = **A1 disable-with-hint** — see the status block.) (FR-064, FR-066, FR-073)

## Phase 1 — Race-free GPU lease (US1, P1) → SC-042 / SC-043 / SC-044

- [ ] **T134** [US1] Implement `serving/gpu_lease.py` — the shared, single-slot, **self-healing** GPU lease
  primitive: an **atomic lockfile** (`os.open` with `O_CREAT|O_EXCL`, PID-stamped) exposing
  `acquire(tenant, est_gb) / release(tenant) / reclaim()` over **separate native processes** (serving,
  training, vision); atomic (no TOCTOU window), auto-reclaimed if a holder exits (on acquire, an unreachable
  recorded PID — `os.kill(pid, 0)` raises — is reclaimed). Stdlib only, no new dep; importable by all three
  native daemons. (FR-062, FR-063, FR-073)
- [ ] **T135** [US1] Add **live-VRAM admission** to the lease/acquire path: read `nvidia-smi` free VRAM at
  acquire and refuse a load whose live footprint exceeds it via the existing oversize path (507 / "exceeds
  VRAM budget") — **replacing** `_fits()`'s file-size estimate as the gate (keep the estimate only as a
  `/health` hint). (FR-064)
- [ ] **T136** [US1] Wire `serving/llama/supervisor.py` (daemon 1 of 3) to the shared `gpu_lease`: acquire
  the lease around `_ensure_loaded()` (the
  existing critical section), release on `_unload()`/idle; **subsume** `_trainer_busy()` so the
  refuse-while-training behavior now flows through the lease with the **same** 507/"GPU busy" semantics.
  Keep idle-release timing and SSE framing byte-identical. (FR-062, FR-071, FR-072)
- [ ] **T137** [US1] Wire `training/trainer.py` (daemon 2 of 3) to the shared `gpu_lease`: acquire the lease
  before a run starts, release on
  completion/failure (the worker's `finally`); **subsume** `_serving_resident()` so the
  refuse-while-serving behavior flows through the lease with the **same 409** "GPU busy" message. (FR-062,
  FR-072)
- [ ] **T138** [P] [US1] **Concurrency/TOCTOU stress test**: fire serving-load + training-start
  concurrently (repeat N times); assert **exactly one** acquires and the other gets the existing "GPU busy"
  rejection — **zero** two-tenant observations. Plus a live-VRAM oversize-refusal test. (SC-042, SC-043)
- [ ] **T139** [P] [US1] **No-regression**: `test_serving` / `test_finetune` / `test_drift_loop` +
  `test_tracing_rest` / `test_tracing_stream` / `test_tracing_resilience`; confirm the lease subsumes both
  guards with unchanged 409/507 codes, SSE frames, tracing, and idle-release. (SC-044, SC-048)

## Phase 2 — Vision on the GPU under the lease (US2, P1) → SC-045

- [ ] **T140** [US2] Move `serving/bento/service.py` (daemon 3 of 3) to load MobileNet onto **cuda**
  (`map_location`/`.to('cuda')`, inference on GPU) as a **lease tenant**, wiring it to the shared `gpu_lease`:
  acquire the lease on `_ensure_loaded()`, hold while classifying, release on idle (mirroring the LLM
  idle-watcher). Reuse `torchvision==0.26.0+cu128` — **no new dependency**, no torch-family movement.
  (FR-066, FR-073)
- [ ] **T141** [US2] Enforce **mutual exclusion** with the LLM via the shared lease: a classify against a
  held lease is refused (the Infer classify control is disabled with a hint, US3) — vision and the LLM are
  **never** co-resident. (FR-067)
- [ ] **T142** [P] [US2] Vision-on-GPU test: classify returns `device: cuda` + the same top-5 shape as the
  CPU path; vision releases on idle; an LLM load while vision holds the lease is refused (never
  co-resident); assert no new dep / no torch-family change. (SC-045)

## Phase 3 — GPU-aware Infer tab (US3, P2) → SC-046 / SC-047

- [ ] **T143** [US3] Gateway: expose **lease/GPU state** (holder tenant, serving `<model>@vN`,
  resident|idle) for the UI — reuse/extend the supervisor `/health` + registry `@serving` resolution;
  **must not** leak the API key to the browser (BFF contract unchanged). (FR-068)
- [ ] **T144** [US3] `ui/app/infer/page.tsx`: **REMOVE** the stream `model:` `<select>` dropdown (it lists
  all models unfiltered and its `selected` value is never sent to `/infer/stream`) and the stale "promote in
  models to switch" hint; replace with a **read-only** `serving: <model>@vN · resident|idle` status line
  sourced from T143. No inference-path behavior change. (FR-069)
- [ ] **T145** [US3] Make `classify` **GPU-state-aware (A1 disable-with-hint)**: disable the dropzone/button
  with an explanatory hint ("GPU busy: LLM resident") when another tenant holds the lease; the operator frees
  the GPU (idle-release or stop serving) to classify. Cooperative refuse-if-held — **no swap/preemption is
  built** (A2 swap-on-demand is the deferred fast-follow). (FR-070)
- [ ] **T146** [P] [US3] `test_ui_smoke` + `test_ui_security`: Infer tab has **no** model dropdown and shows
  the status line; classify is **disabled with a hint** while a tenant holds the lease (A1); BFF contract
  intact (key absent from browser payloads, allowlist + origin guard). (SC-046, SC-047)

## Phase 4 — Cross-cutting regression → SC-048

- [ ] **T147** Full 001/006 keyed sweep green with the lease in place: LLM serving, training, tracing, SSE
  framing, idle-release, the six UI tabs + BFF — all **byte-identical** behavior to pre-008 (409/507 codes,
  SSE frames). (SC-048)
- [ ] **T148** Confirm the **GPU stack is still frozen** — no torch/torchvision/transformers/peft/
  accelerate/datasets movement; `scripts/native_env.lock` unchanged except (if any) the cuda-device flip
  for vision (no version change). (FR-073)
- [ ] **T149** [P] Re-verify the **one-tenant invariant** end-to-end across all three real tenants (LLM,
  vision, training) under interleaved load — exactly one resident at any instant, self-healing on a killed
  holder. (SC-042, FR-063)
- [ ] **T150** [P] Re-confirm **live-VRAM admission** governs (an oversize load refused against real free
  VRAM), and the `_fits()` estimate is no longer the gate (only a hint). (SC-043)
- [ ] **T151** [P] Re-confirm **idle-release + on-demand load** for every tenant (LLM + vision): nothing
  always-on, each frees VRAM on idle, next acquire cold-loads. (FR-071)
- [ ] **T152** [P] Re-confirm the **constitution gate**: Principle II reads v1.4.0 (ratified at T132), still
  NON-NEGOTIABLE; I/III unaffected; VI strengthened (lease state exposed). (Constitution Check)
- [ ] **T153** Commit the increment: `gpu_lease.py` lockfile module + daemon/vision wiring + Infer UI +
  tests; the resolved grill decisions (atomic lockfile; A1 disable-with-hint) are already recorded in the
  spec/plan/tasks status blocks.

---

## Dependencies & Execution Order

- **T132 (ratify v1.4.0) + T133 (pre-flight) gate everything** — the two grill items are resolved (atomic
  lockfile; A1 disable-with-hint), so building proceeds once the amendment is ratified and pre-flight passes.
- **US1 (lease, T134–T139)** is the foundation; US2 and US3 both depend on the lease existing.
- **US2 (vision-on-GPU, T140–T142)** depends on US1 (it acquires the same lease).
- **US3 (Infer tab, T143–T146)** depends on US1 (state to render) and US2 (the disabled-classify hint
  targets a real GPU tenant).
- **Phase 4 (T147–T153) lands last** (needs all three stories in place).

### Constitution gates (re-check each phase)
- Principle II **amended → v1.4.0** (ratified T132): generalized to a single race-free GPU lease; still
  exactly one tenant in VRAM; NON-NEGOTIABLE.
- Principle I/III unaffected: host-local lease, no new service/runtime/dependency, frozen GPU stack.
- Principle VI strengthened: lease/GPU state exposed + live-VRAM admission.

## Implementation Strategy

1. **Ratify v1.4.0**, then build the **atomic-lockfile lease** (`gpu_lease.py`) and wire the two daemons to
   subsume their guards with live-VRAM admission. **Stop and validate** the one-tenant invariant
   under concurrency + the 001/006 no-regression.
2. **Move vision onto the GPU** as a lease tenant; validate device=cuda + mutual exclusion + no new dep.
3. **Make the Infer tab truthful**: status line in place of the dropdown; classify disabled-with-hint when
   a tenant holds the lease (A1).
4. Each phase re-runs the relevant 001/006 tests on the target GPU; never regress LLM/training behavior;
   never move the frozen GPU stack.

## Out of Scope (recorded)
- **ASR serving** — deferred to **009**; 008 builds the modality-general lease and moves only
  vision onto it.
- **Multi-GPU / concurrent residency** — still exactly one tenant in VRAM (a Principle II violation
  otherwise, not an amendment).
- **Frozen GPU stack upgrade** (torch/torchvision/transformers/…): unchanged (007 FR-060 / FR-073).
- **LLM/training contract changes** — 409/507 codes, SSE framing, and 006 tracing are preserved exactly.
- **A2 swap-on-demand / preemptive eviction** — DEFERRED as a documented fast-follow; 008 is cooperative
  **refuse-if-held** (classify disabled-with-hint), not preemptive (no unload-now/swap orchestration).
- **A general resource scheduler / fairness queue** — 008 is a single-slot mutex done race-free, not a
  multi-tenant scheduler.
