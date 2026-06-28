# Feature Specification: Race-Free GPU Lease & Vision-on-GPU

**Feature Branch**: `008-gpu-lease`

**Created**: 2026-06-28

**Status**: **BUILT & VALIDATED ON HARDWARE (2026-06-28)** — feat/008-gpu-lease; constitution ratified
to v1.4.0. Atomic lockfile lease (flock-guarded O_CREAT|O_EXCL + os.kill reclaim) closes the TOCTOU
race; vision served on device=cuda under the lease, never co-resident with the LLM; Infer tab
status-line + classify-disable-with-hint. See tasks.md status block for the validation evidence.

**Grilled decisions (2026-06-28):**
1. **Lease primitive = ATOMIC LOCKFILE** (resolves FR-062's open fork). A shared PID-stamped lockfile on
   the WSL Ubuntu filesystem, acquired via `O_CREAT|O_EXCL` (atomic create), with stale-holder reclamation
   (on acquire, if the recorded PID is not alive — `os.kill(pid, 0)` raises — reclaim it; satisfies the
   FR-063 self-healing requirement). The lock is acquired by the **three native WSL daemons** (serving
   supervisor, trainer, bento/vision) — the actual GPU holders — before any GPU work; they self-arbitrate
   via a shared stdlib `gpu_lease.py` module. *Rationale*: the GPU is held by native WSL daemons sharing
   the WSL filesystem; the gateway is a Docker container that only proxies and never touches the GPU, and
   training is fire-and-forget (gateway returns while the run continues), so an in-gateway asyncio
   semaphore can't gate long-running native work and a gateway-brokered token would couple GPU admission to
   the gateway being up + make it stateful (lease lost on gateway restart). The lockfile is stdlib-only (no
   new dep, Principle III), survives a gateway restart, and the gateway reads the current holder via the
   daemons' `/health` purely for the UI status line (FR-068) — it does NOT arbitrate.
2. **Classify-on-busy = A1 (DISABLE WITH HINT)** (resolves FR-070's A1-vs-A2 open fork). When another
   tenant holds the lease, the Infer tab's `classify` control is disabled with an explanatory hint ("GPU
   busy: LLM resident"); the operator frees the GPU (idle-release or stop serving) to classify — fitting
   the cooperative **refuse-if-held** lease exactly, with NO preemption/eviction built. **A2 (swap-on-
   demand)** is explicitly DEFERRED as a documented fast-follow (it would upgrade the lease to preemptive —
   unload-now command + swap orchestration + evict/reload thrash — out of 008 scope).

**Input**: Roadmap follow-on to the hardened, refreshed platform (002/004/005/006/007). Today the
"one model in VRAM" rule is enforced by **two independent, cooperating HTTP guards**: the trainer
refuses to start while a serving model is resident (`trainer.py` → `_serving_resident()` 409), and the
serving supervisor refuses to load while a training run is active (`supervisor.py` → `_trainer_busy()`).
That pair works, but it is a **time-of-check/time-of-use (TOCTOU) race** — two callers can each poll
"the other is idle", both see free, and both proceed — and it estimates VRAM **statically** from file
size (`_fits()`), not from the live GPU. It also only covers LLM↔training; **vision runs CPU-only by
design**, sitting outside the rule entirely. 008 generalizes the mutex into a single **race-free GPU
lease**: exactly one GPU tenant resident at any instant — {LLM, vision, ASR} OR a training
run — admitted atomically against **live free-VRAM**. (ASR *serving* arrives in 009; 008 builds
the lease and moves **vision** onto it.)

> **Scope note**: 008 is a **concurrency-correctness + GPU-admission** increment. It adds **no new
> lifecycle stage and no new service**. It generalizes the existing one-model-in-VRAM mutex into an
> atomic, live-VRAM-checked lease; moves vision from CPU onto the GPU as a lease tenant; and makes the
> Infer tab GPU-state-aware (drop the misleading dropdown → status line; classify disabled-with-hint when held).
> Requirement IDs continue the shared space (FR-062+, SC-042+, tasks T132+). **This increment carries a
> constitution amendment** — Principle II `v1.3.0`→**`v1.4.0`** — that *generalizes* (does not weaken)
> the one-model-in-VRAM rule into a one-GPU-tenant lease. The amendment is embedded verbatim in plan.md
> and ratified by a Phase-0 task; the live `constitution.md` is **not** edited here (build-time
> ratification, exactly as 003 did for v1.3.0).

> **Hard boundary (NON-NEGOTIABLE)**: behavior for **LLM serving and training is preserved** — the lease
> must SUBSUME today's two guards without regressing the 001/006 tests (same 409/507 semantics, same
> idle-release, same SSE framing, same tracing). The **Blackwell GPU stack stays frozen** (007 FR-060):
> vision-on-GPU reuses the existing `torchvision==0.26.0+cu128` already in the native env — **no new
> dependency**.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — A single race-free GPU lease (Priority: P1)

The two cooperating guards become **one lease**: a single-slot admission mechanism such that exactly ONE
GPU tenant is resident at any instant. Acquiring the lease is **atomic** (closes the TOCTOU race — no
window where two callers each see "other idle" and both proceed), and admission checks **live free VRAM
from `nvidia-smi`**, not a static file-size estimate. The lease subsumes the trainer's
refuse-while-serving-resident guard and the supervisor's refuse-while-training guard, preserving their
observable 409/507 behavior.

**Why this priority**: This is the load-bearing correctness fix and the foundation the other two stories
build on. Principle II is the platform's defining constraint (NON-NEGOTIABLE); a race in its enforcement
is the highest-severity latent bug. Live-VRAM admission also replaces a guess with ground truth.

**Independent Test**: Fire a serving-load and a training-start **concurrently** (and, after US2, a
vision-classify against a held lease); exactly one acquires the GPU and the loser gets the existing
"GPU busy" rejection — never two tenants resident. A model whose live footprint would exceed free VRAM is
refused with the existing 507 path. The full 001/006 suite (serving, training, tracing, SSE framing,
idle-release) passes unchanged.

**Acceptance Scenarios**:

1. **Given** an idle GPU, **When** a serving-load and a training-start race for the lease at the same
   instant, **Then** exactly one acquires it and proceeds; the other is rejected with the current
   "GPU busy" message (409) — **never** both resident (no TOCTOU window).
2. **Given** a tenant holding the lease, **When** a second tenant requests it, **Then** it is refused
   until the holder releases (idle-release for serving/vision; run-completion for training) — the
   release path is unchanged from today.
3. **Given** a model whose **live** resident footprint would exceed free VRAM (read from `nvidia-smi` at
   admission, replacing the static `_fits()` estimate), **When** a load is attempted, **Then** it is
   refused with the existing oversize path (507 / "exceeds VRAM budget"), now grounded in real free VRAM.
4. **Given** the lease in place, **When** the full 001/006 suite runs, **Then** LLM serving, training,
   tracing, SSE framing, and idle-release all behave **identically** (the lease subsumes both guards
   with no behavior regression).

---

### User Story 2 — Vision served on the GPU under the lease (Priority: P1)

The MobileNet vision classifier moves from CPU-only onto the **GPU as a lease tenant**: when it serves, it
holds the single lease (strict one-model-in-VRAM now **includes vision**), loads on demand, and releases
on idle — exactly like the LLM. It reuses the existing `torchvision` cu128 stack (no new dependency).

**Why this priority**: Vision is the second modality and the proof that the lease is truly modality-general
(not LLM-special). It validates eviction/admission across two real tenants before ASR arrives in
009, and it removes the CPU-only carve-out that the v1.4.0 amendment closes.

**Independent Test**: Classify an image; the vision model loads onto the GPU under the lease (device
reports CUDA, not CPU), returns the same top-5 shape, and releases on idle. While vision holds the lease,
an LLM load is refused (cooperative refuse-if-held) — never co-resident. The LLM serving path is unchanged
when vision is idle.

**Acceptance Scenarios**:

1. **Given** an idle GPU, **When** an image is classified, **Then** the vision model acquires the lease,
   runs on the **GPU** (device = cuda), returns the same top-5 prediction shape as the CPU path, and
   releases the lease after its idle timeout.
2. **Given** vision holding the lease, **When** an LLM inference is requested, **Then** it is refused
   (cooperative refuse-if-held) — vision and the LLM are **never** co-resident.
3. **Given** the vision-on-GPU move, **When** `/vision/classify` runs end-to-end, **Then** it reuses the
   existing `torchvision==0.26.0+cu128` stack with **no new dependency** and no torch-family movement
   (007 FR-060 still holds).

---

### User Story 3 — GPU-aware Infer tab (Priority: P2)

The Infer tab stops lying about model selection and starts reflecting GPU state. The misleading stream
`model:` dropdown is **removed** (today it lists every registered model AND its selection is never sent to
`/infer/stream` — the resident GGUF always serves), replaced by a **read-only status line** ("serving:
`<model>@vN` · resident|idle"). The `classify` action becomes **GPU-state-aware**: when another tenant
holds the lease, classify is **disabled with an explanatory hint** ("GPU busy: LLM resident") and the
operator frees the GPU (idle-release or stop serving) to classify — the cooperative refuse-if-held lease,
with no preemption. (Swap-on-demand — evict the holder, load vision, ~2.5s — is the deferred A2 fast-
follow, out of 008 scope.)

**Why this priority**: It is the user-visible truthfulness fix once the lease exists. The dropdown is
actively misleading (it implies a selection that has no effect); the status line tells the operator what is
actually resident. Lower priority than the correctness/foundation work, so P2.

**Independent Test**: The Infer tab renders a read-only "serving: …" status line (no model dropdown); the
status reflects resident vs idle from the gateway. With the LLM holding the lease, the classify control is
disabled with an explanatory hint ("GPU busy: LLM resident"); the operator frees the GPU to classify. The
BFF security contract (key never in payloads, allowlist, origin guard) is unchanged.

**Acceptance Scenarios**:

1. **Given** the Infer tab, **When** it loads, **Then** there is **no** stream `model:` dropdown; instead a
   read-only status line shows `serving: <model>@vN · resident|idle` sourced from the gateway's GPU/lease
   state.
2. **Given** the LLM holds the lease, **When** the operator attempts to classify an image, **Then** the
   classify control is **disabled with a hint** that the GPU is busy with the LLM ("GPU busy: LLM
   resident"); the operator must free the GPU (idle-release or stop serving) before classifying — no
   preemption/swap is offered (the cooperative refuse-if-held lease).
3. **Given** the tab changes, **When** the UI security tests run, **Then** the BFF contract is unchanged
   (API key absent from all browser-visible payloads, route allowlist + same-origin guard intact).

---

### Edge Cases

- **TOCTOU under true concurrency**: two callers polling "is the other idle?" both see idle and both
  proceed — the exact race 008 closes. The lease acquire MUST be atomic (a `O_CREAT|O_EXCL` lockfile),
  not a check-then-act over two HTTP health probes (FR-062).
- **Live VRAM vs static estimate**: `_fits()` estimates from file size; a fragmented or partially-occupied
  GPU can have *less* free VRAM than the estimate assumes. Admission MUST read **live** free VRAM from
  `nvidia-smi` so an oversize load is caught against reality (FR-064).
- **Vision now counts**: once vision is a GPU tenant, classifying while the LLM is resident must NOT load a
  second model — it is refused (the Infer classify control is disabled with a hint, US3), never co-resident
  (FR-066).
- **Preemption is out of scope**: 008 is a cooperative **refuse-if-held** lease — there is **no swap/evict**
  in this increment. A held lease blocks the other tenant until the holder releases; the operator frees the
  GPU to proceed. Swap-on-demand (A2) — evict-then-load (~2.5s) behind a confirm — is the deferred fast-
  follow (FR-070), not built here.
- **Stale dropdown removed**: the removed dropdown's `selected` value was never sent to the backend; nothing
  downstream depends on it, so its removal changes no inference behavior (FR-069).
- **No behavior regression**: the lease must subsume both current guards with byte-identical SSE framing,
  the same 409 "GPU busy" / 507 "exceeds VRAM" codes, and the same idle-release timing — the 001/006 suite
  is the guardrail (SC-048).
- **Lease holder crash**: if a tenant dies holding the lease (process exit), the lease MUST become
  re-acquirable (no permanent deadlock) — stale-lease reclamation is part of the chosen primitive (FR-063).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-062**: GPU admission MUST be governed by a **single, atomic lease** — exactly one GPU tenant
  resident at any instant. Acquiring the lease MUST close the current TOCTOU race (no window in which two
  callers each observe "other idle" and both proceed). The lease MUST subsume **both** existing guards: the
  trainer's refuse-while-serving-resident (`trainer.py` 409) and the supervisor's refuse-while-training
  (`supervisor.py`), preserving their observable rejection semantics. The lease primitive is an **atomic
  lockfile** on the WSL Ubuntu filesystem — a PID-stamped file created via `O_CREAT|O_EXCL` (atomic
  create), with stale-holder reclamation (on acquire, an unreachable recorded PID — `os.kill(pid, 0)`
  raises — is reclaimed; see FR-063). It is acquired by the **three native WSL daemons** (serving
  supervisor, trainer, bento/vision) — the actual GPU holders — before any GPU work; they self-arbitrate
  via a shared stdlib `gpu_lease.py` module. The Docker gateway never touches the GPU and does NOT
  arbitrate; it only **reads** the current holder via the daemons' `/health` for the UI status line
  (FR-068).
- **FR-063**: The lease MUST be **single-slot** and **self-healing**: a tenant that exits while holding the
  lease MUST not deadlock the GPU — the lease becomes re-acquirable (stale-holder reclamation), so a crashed
  serving/vision process or failed run frees the slot.
- **FR-064**: Admission MUST check **live free VRAM** read from `nvidia-smi`
  (`--query-gpu=memory.free`) at acquire time, **replacing** the static file-size `_fits()` estimate in
  `supervisor.py`. A load whose live footprint would exceed free VRAM MUST be refused via the existing
  oversize path (507 / "exceeds VRAM budget"), now grounded in real free VRAM.
- **FR-065**: The lease MUST be **modality-general**: any GPU-resident tenant — LLM, **vision**, and
  (in 009) ASR — OR a training run holds the same single slot. CPU-only models (e.g. embeddings, tabular)
  are exempt and never take the lease.
- **FR-066**: The vision classifier MUST run as a **GPU lease tenant**: load on demand onto the GPU, hold
  the single lease while serving (strict one-model-in-VRAM now including vision), and release on idle —
  mirroring the LLM supervisor. It MUST reuse the existing `torchvision==0.26.0+cu128` stack with **no new
  dependency** and no torch-family movement (007 FR-060).
- **FR-067**: Vision-on-GPU and the LLM MUST be **mutually exclusive in VRAM** under the lease — never
  co-resident. A classify against a held lease is refused (the classify control is disabled with a hint per
  FR-070), exactly as an LLM load against a held lease is.
- **FR-068**: The gateway MUST expose **lease/GPU state** (which tenant holds the lease, the serving model
  name + version, resident|idle) so the UI can render a truthful status line. This SHOULD reuse/extend the
  existing supervisor `/health` + registry `@serving` resolution; it MUST NOT leak the API key to the
  browser (BFF contract unchanged).
- **FR-069**: The Infer tab MUST **remove** the stream `model:` dropdown (it lists all models unfiltered
  and its `selected` value is never sent to `/infer/stream`) and replace it with a **read-only status
  line** rendering `serving: <model>@vN · resident|idle` from FR-068. No inference behavior changes (the
  resident GGUF already serves regardless of the old selection).
- **FR-070**: The Infer tab's `classify` action MUST be **GPU-state-aware** when another tenant holds the
  lease — **A1 (disable-with-hint)**: the control is disabled with an explanatory hint ("GPU busy: LLM
  resident") and the operator frees the GPU (idle-release or stop serving) to classify. This fits the
  cooperative **refuse-if-held** lease exactly — **no preemption/eviction is built**. *(A2 swap-on-demand —
  evict the holder, load vision, ~2.5s, behind a cost-stating confirm — is explicitly DEFERRED as a
  documented fast-follow; it would upgrade the lease to preemptive, out of 008 scope.)*
- **FR-071**: The lease MUST preserve **idle-release** and **on-demand load** for every tenant (Principle
  II): nothing is always-on, each tenant frees VRAM after its idle timeout, and the next acquire cold-loads
  — unchanged from today for the LLM and now applied to vision.
- **FR-072**: The change MUST be **behavior-preserving for LLM serving and training**: identical 409
  "GPU busy" and 507 "exceeds VRAM" semantics, identical SSE framing (byte-identical frames), identical
  006 tracing (span-outside-the-lock, fire-and-forget, fail-open), and identical idle-release timing —
  validated by the un-modified 001/006 tests.
- **FR-073**: **No new dependency** is introduced and the **Blackwell GPU stack stays frozen** (007
  FR-060): torch/torchvision/transformers/peft/accelerate/datasets are unchanged; vision-on-GPU uses the
  cu128 torchvision already present. The chosen lease primitive MUST use only the existing runtimes
  (Python stdlib / the gateway's current deps).

### Key Entities *(include if feature involves data)*

- **GpuLease**: the single-slot admission token. At most one holder at any instant; records the current
  tenant (LLM | vision | training) and is self-healing on holder exit (FR-062/FR-063).
- **LeaseTenant**: a GPU-resident modality or a training run that must hold the lease while using VRAM —
  {LLM, vision, (009: ASR)} OR a training run. CPU-only models (e.g. embeddings, tabular) are **not** tenants (exempt).
- **LiveVramReading**: the `nvidia-smi` free-VRAM value read at acquire time; the admission ground truth
  that replaces the static `_fits()` estimate (FR-064).
- **LeaseState (UI projection)**: the gateway-exposed view the Infer status line renders — holder tenant,
  serving `<model>@vN`, resident|idle (FR-068/FR-069).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-042**: Under concurrent serving-load + training-start (and vision-classify) requests, **exactly
  one** acquires the GPU and the others are rejected with the existing "GPU busy" message — **zero**
  observations of two tenants resident across a repeated concurrency stress (the TOCTOU race is closed).
- **SC-043**: Admission decisions use **live `nvidia-smi` free VRAM**; an oversize load is refused against
  real free VRAM via the existing 507 path, and the static `_fits()` estimate is no longer the gate.
- **SC-044**: The lease **subsumes both** prior guards — the trainer's refuse-while-serving and the
  supervisor's refuse-while-training behaviors are reproduced through the single lease with the same 409/507
  codes; the 001/006 suite passes unchanged.
- **SC-045**: Vision classification runs **on the GPU** under the lease (device = cuda), returns the same
  top-5 shape as the CPU path, releases on idle, and is **never** co-resident with the LLM — with **no new
  dependency** and no torch-family movement.
- **SC-046**: The Infer tab shows a **read-only** `serving: <model>@vN · resident|idle` status line and has
  **no** model dropdown; the status reflects real lease/GPU state from the gateway.
- **SC-047**: The `classify` control is GPU-state-aware — **disabled with an explanatory hint** ("GPU busy:
  LLM resident") when the LLM holds the lease; the BFF security contract is unchanged (key absent from
  browser payloads).
- **SC-048**: **No regression** — the full 001/006 suite (serving, training, tracing, SSE framing,
  idle-release, the six UI tabs + BFF) passes; LLM/training behavior, status codes, and SSE frames are
  byte-identical to pre-008.

## Assumptions

- **The lease generalizes, not weakens, Principle II** — one GPU tenant at a time is the *same* constraint
  as one model in VRAM, extended to cover any modality plus training. The v1.4.0 amendment records this
  generalization; the rule stays NON-NEGOTIABLE.
- **Live VRAM is the right gate** — `nvidia-smi --query-gpu=memory.free` is already used by `trainer.py`
  (`_gpu_free_mib()`); 008 extends that read into the admission decision, so no new tool/dep is needed.
- **Vision-on-GPU is cheap and reuses cu128 torchvision** — MobileNet is tiny; moving it to CUDA reuses the
  frozen torchvision stack already installed in the native env, costing no new dependency and trivial VRAM.
- **The old dropdown is inert** — its `selected` value is never sent to `/infer/stream`; removing it changes
  no inference behavior. The status line is strictly an improvement in truthfulness.
- **Single local operator, unchanged posture** — loopback binding, fail-closed gateway auth, and the BFF
  contract (005/004) all stand; 008 changes admission concurrency + one modality + one UI surface, nothing
  about the network/security posture.

## Non-Goals

- **ASR serving** — that modality arrives in **009**; 008 builds the lease and moves only
  **vision** onto it. The lease is *designed* modality-general, but only LLM + vision + training are wired
  in this increment.
- **Multi-GPU / concurrent residency** — still exactly one tenant in VRAM; 008 does not introduce any
  multi-model-in-VRAM capability (that would be a Principle II violation, not an amendment).
- **Changing the LLM/training contracts** — the gateway endpoints, status codes, SSE framing, and 006
  tracing are preserved exactly; 008 is a concurrency + admission + vision-placement change, not an API
  redesign.
- **Touching the frozen GPU stack** — no torch/torchvision/transformers/peft/accelerate/datasets movement
  (007 FR-060); vision-on-GPU reuses what is already installed.
- **A2 swap-on-demand / preemptive eviction** — DEFERRED as a documented fast-follow. 008 is a cooperative
  **refuse-if-held** lease (a held lease blocks the other tenant until release; the operator frees the GPU).
  A2 would upgrade it to preemptive — an unload-now command + swap orchestration + evict/reload thrash —
  which is out of 008 scope.
- **A general resource scheduler / queue with fairness** — 008 is a single-slot mutex done race-free, not a
  multi-tenant scheduler; queuing/fairness beyond "refuse if held" is out of scope.
