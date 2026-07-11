# Implementation Plan: Race-Free GPU Lease & Vision-on-GPU

**Branch**: `008-gpu-lease` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/008-gpu-lease/spec.md` (generalize the one-model-in-VRAM
mutex into a race-free, live-VRAM-checked GPU lease; move vision onto the lease; make the Infer tab
GPU-aware)

## Summary

Generalize today's two cooperating guards — `trainer.py` refuse-while-serving-resident and
`supervisor.py` refuse-while-training — into **one atomic, single-slot GPU lease** that closes the
current TOCTOU race and admits against **live `nvidia-smi` free VRAM** instead of the static `_fits()`
estimate (US1). Move the **vision** classifier from CPU-only onto the GPU as a lease tenant, reusing the
frozen cu128 torchvision (US2). Make the **Infer tab** truthful and GPU-aware: drop the misleading stream
`model:` dropdown for a read-only `serving: <model>@vN · resident|idle` status line, and disable `classify`
with a hint when another tenant holds the lease (US3). Behavior for LLM serving and training is
**preserved**; the GPU stack stays **frozen**; **no new dependency**. This increment carries a
constitution amendment — Principle II `v1.3.0`→**`v1.4.0`** — that *generalizes* (does not weaken) the
rule. Phase-gated like 002/004/005/006/007, validated against the full 001/006 suite, never regressing.

## Technical Context

**Language/Version**: Python 3.12 (gateway, per 007), Python stdlib (native supervisor/trainer daemons),
Node 20+ (UI/BFF). **No new language or runtime.**

**Primary Dependencies**: none added. The lease primitive is an **atomic lockfile** implemented in a shared
stdlib `gpu_lease.py` module (`os.open` with `O_CREAT|O_EXCL`, PID stamp, `os.kill(pid, 0)` stale-holder
reclamation) — stdlib only, no FastAPI/asyncio and no gateway round-trip on the GPU host. Vision-on-GPU
reuses `torchvision==0.26.0+cu128` already installed in the native env. **Frozen (007 FR-060)**: torch
`2.11.0+cu128`, torchvision `0.26.0+cu128`, transformers, peft, accelerate, datasets.

**Live-VRAM-admission note (FR-064)**: `trainer.py` already shells `nvidia-smi
--query-gpu=memory.free --format=csv,noheader,nounits` (`_gpu_free_mib()`). 008 lifts that read into the
**acquire** decision in `supervisor.py` (and the vision tenant), replacing `_fits()`'s file-size estimate
as the gate. The estimate may stay as a *hint* in `/health`, but admission is the live reading.

**Lease primitive (GRILLED — atomic lockfile)**: a PID-stamped lockfile on the WSL Ubuntu filesystem,
acquired via `O_CREAT|O_EXCL` (atomic create), with `os.kill(pid, 0)` stale-holder reclamation on acquire
(FR-063). *Implementation note (build):* the read-decide-claim window is serialized by an `fcntl.flock`
on a persistent sidecar so stale reclamation cannot race (two reclaimers must not both clobber a freshly
re-acquired holder, which would re-open the very double-hold the lease exists to prevent); the flock also
auto-releases on a holder's death, a kernel-level backstop to the `os.kill` check. flock + O_EXCL are
belt-and-suspenders realizations of the same "atomic host lockfile, self-healing" intent — both stdlib, no
new dep. It is **cross-process** — the serving supervisor, trainer, and vision are *separate native
processes* that share the WSL filesystem and self-arbitrate via the shared `serving/gpu_lease.py` module;
no gateway round-trip on the GPU host. *Rejected*: **(b) gateway-brokered token** — the Docker gateway only
proxies and never touches the GPU, and training is fire-and-forget (the gateway returns while the run
continues), so a token would couple GPU admission to the gateway being up + make it stateful (lease lost on
gateway restart); **(c) in-gateway `asyncio.Queue`/semaphore** — can't gate long-running native work across
*separate OS processes* at all. The gateway only **reads** the holder via the daemons' `/health` for the UI
status line (FR-068); it does NOT arbitrate.

**Classify-on-busy (GRILLED — A1 disable-with-hint)**: when another tenant holds the lease the classify
control is **disabled with an explanatory hint** ("GPU busy: LLM resident") and the operator frees the GPU
(idle-release or stop serving) to classify — fitting the cooperative refuse-if-held lease, with no
preemption. **A2 swap-on-demand** (evict holder → load vision, ~2.5s, behind a cost-stating confirm) is
DEFERRED as a fast-follow (it would upgrade the lease to preemptive — out of 008 scope).

**Storage**: none changed. No DB/schema/volume change; the lease is in-memory/lockfile state, not
persisted.

**Target Platform**: Win11 + WSL2 + Rancher Desktop. The gateway/MLflow/infra run in Docker; the serving
supervisor, trainer, vision (bento), and UI run **native in WSL**. The lease must serialize the **native
GPU processes** (serving, training, vision) — a key constraint on the primitive choice.

**Project Type**: concurrency-correctness + GPU-admission change over 002/004/005/006/007. Touches the two
native daemons (`serving/llama/supervisor.py`, `training/trainer.py`), the vision service
(`serving/bento/service.py`), a new shared lease module, the gateway state-exposure
(`gateway/app/serving.py` / a small lease/state surface), the Infer UI (`ui/app/infer/page.tsx`), and the
tests. **No torch-family or dependency change.**

**Performance Goals**: the lease acquire/release MUST NOT add measurable latency to the inference hot path
(the LLM acquire is already gated by `_gpu_lock`; a lockfile create/unlink is sub-millisecond). No swap
exists in 008 (refuse-if-held); the deferred A2 swap would be ~2.5s evict+load and operator-initiated.

**Constraints**: behavior-preserving for LLM/training (identical 409/507, SSE framing, tracing,
idle-release); exactly one GPU tenant (Principle II, now v1.4.0); GPU stack frozen; no new dependency;
loopback/auth/BFF posture unchanged.

## Constitution Amendment (v1.4.0)

008 **amends Principle II**. The amendment **generalizes** the rule from "a single LLM model resident with
a serving↔training mutex" to "a single GPU lease held by exactly one tenant, race-free". This is a
strengthening generalization — the constraint stays NON-NEGOTIABLE and the VRAM budget, on-demand load,
and idle-release are all retained. Per the workflow rule ("no deviation without a documented amendment"),
the amendment text is embedded here verbatim and ratified by a Phase-0 task (**T132**); the **live
`constitution.md` is NOT edited in this increment** — ratification is a build-time task, exactly as 003 did
for v1.3.0.

**Amended Principle II (verbatim — proposed v1.4.0):**

> ### II. Single-GPU, On-Demand Serving (NON-NEGOTIABLE)
> At most ONE GPU tenant may be resident in GPU VRAM at any instant — any GPU-resident modality (LLM,
> vision, ASR, …) **or** a training run — enforced by a **single, race-free GPU lease**: a
> single-slot admission mechanism with no time-of-check/time-of-use window, so two callers can never both
> proceed onto the GPU. CPU-only models (e.g. embeddings, tabular) hold no lease and are exempt. Tenants load on
> request and release VRAM after use (idle-release); workers are not always-on. Admission is checked
> against **live free VRAM** and no feature may assume more VRAM than the host GPU provides (`VRAM_GB` in
> [hardware-profile.md](../../.specify/memory/hardware-profile.md)). This single-tenant GPU lease is the core constraint that
> separates this platform from production cluster designs — violating it defeats the project's purpose.

**Amendment metadata to apply at ratification (T132):** bump `**Version**: 1.3.0` → `1.4.0`, set
`**Last Amended**: 2026-06-28`, and append a `v1.4.0` note to the changelog comment:
`v1.4.0: Principle II generalized from "one LLM model in VRAM + serving↔training mutex" to "one GPU
tenant under a single race-free lease — any GPU-resident modality OR a training run; live-VRAM admission;
CPU-only models exempt." A strengthening generalization; the rule stays NON-NEGOTIABLE. On-demand load +
idle-release + VRAM budget retained.`

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Lease is host-local (atomic lockfile on the WSL filesystem); nothing leaves the host; loopback posture unchanged | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | **Amended v1.3.0→v1.4.0** — generalized to a single race-free GPU lease over any one tenant; on-demand + idle-release + VRAM budget retained; still exactly one in VRAM | ✅ **amended (v1.4.0), strengthened** |
| III. Lightweight Footprint | No new service/runtime/dep; vision-on-GPU reuses installed cu128 torchvision; lease is stdlib | ✅ unaffected |
| IV. Full Lifecycle Coverage | No stage added/dropped; serving still spans LLM + vision | ✅ N/A |
| V. OSS & Swappable | Same OSS components; lease is a thin admission layer behind the existing interfaces | ✅ |
| VI. Reproducibility & Observability | Lease/GPU state is **exposed** (status line + health); admission grounded in live VRAM — observability improved | ✅ strengthened |
| VII. Phase-Gated Delivery | Three independently-runnable stories (US1 lease, US2 vision-on-GPU, US3 UI), each re-validated on the target GPU | ✅ |
| Workflow: "no deviation without amendment" | Principle II generalized → **amendment embedded + ratified (T132)**; live constitution.md untouched until ratify, as 003 did | ✅ amendment present |

**Amendment required and present (Principle II → v1.4.0).** Every other principle is clean: I (loopback)
and III (footprint, no new dep) are unaffected; VI is advanced by exposing lease state + live-VRAM
admission. The amendment *generalizes* (does not weaken) the non-negotiable, mirroring how 003 amended for
the UI runtime.

## Project Structure

### Source Code (delta over 007)

```text
mlops-lite/
├── serving/
│   ├── llama/supervisor.py        # MODIFIED: acquire/release the lease around load; live-VRAM admission
│   │                              #           replaces _fits() as the gate; subsumes _trainer_busy() (FR-062/064)
│   └── bento/service.py           # MODIFIED: vision becomes a GPU lease tenant — load to cuda, hold lease,
│   │                              #           idle-release; reuse cu128 torchvision, NO new dep (FR-066)
│   └── gpu_lease.py               # NEW (shared, stdlib): the single-slot, self-healing GPU lease primitive —
│                                  #         atomic O_CREAT|O_EXCL lockfile, PID-stamped, os.kill(pid,0) stale
│                                  #         reclaim; acquire/release/reclaim; imported by all 3 native daemons
├── training/trainer.py           # MODIFIED: acquire/release the lease around a run; subsumes _serving_resident()
│                                  #           guard, same 409 semantics (FR-062)
├── gateway/app/
│   ├── serving.py                # MODIFIED: surface lease/GPU state (holder, serving model@vN, resident|idle)
│   │                              #           for the UI; behavior-preserving for /infer + /infer/stream (FR-068/072)
│   └── routers/vision.py         # MODIFIED (light): classify proxy unchanged on the wire; may surface lease state
├── ui/app/infer/page.tsx         # MODIFIED: REMOVE stream model dropdown → read-only "serving: <model>@vN ·
│                                  #           resident|idle" status line; classify disabled-with-hint when held (A1) (FR-069/070)
└── tests/                        # NEW: concurrency/TOCTOU stress (one-tenant invariant), live-VRAM admission,
                                  #      vision-on-GPU device=cuda + mutual-exclusion, Infer status-line/classify,
                                  #      full 001/006 no-regression sweep
```

**Structure Decision**: put the lease in **one shared `serving/gpu_lease.py`** so the three native GPU
processes (serving, training, vision) acquire the *same* atomic lockfile slot — the cross-process reality
is the central design constraint and the reason an in-gateway primitive is insufficient (it can't serialize
separate native daemons, and the gateway never holds the GPU). Keep
`supervisor.py`/`trainer.py`/`service.py` edits to "acquire around the existing critical section" so a
regression bisects cleanly and the 001/006 behavior is preserved.

## Phasing (maps to constitution VII)

- **Phase 0 — Ratify + pre-flight (T132–T133)**: **ratify the v1.4.0 amendment** (bump constitution
  Principle II + version/changelog per the metadata above — the build-time ratify, as 003 did); confirm
  `nvidia-smi --query-gpu=memory.free` reads cleanly on the WSL host; confirm cu128 torchvision present for
  vision-on-GPU. (The two grill items — lease primitive, classify-on-busy — are **resolved**: atomic
  lockfile + A1 disable-with-hint; see *Technical Context* and *Grilled decisions*.)
- **Phase 1 — Race-free lease (US1, P1) → SC-042/043/044**: implement `serving/gpu_lease.py` (atomic
  lockfile, single-slot, self-healing); wire `supervisor.py` + `trainer.py` to acquire/release around
  their existing critical sections, subsuming both guards; replace `_fits()` admission with live-VRAM read;
  concurrency/TOCTOU stress + 001/006 no-regression. Exit: one-tenant invariant holds under stress, codes
  unchanged.
- **Phase 2 — Vision on the GPU (US2, P1) → SC-045**: move `service.py` to load MobileNet onto **cuda**
  under the lease, hold while serving, idle-release; verify device=cuda, same top-5 shape, never co-resident
  with the LLM, no new dep. Exit: vision is a lease tenant.
- **Phase 3 — GPU-aware Infer tab (US3, P2) → SC-046/047**: expose lease/GPU state from the gateway
  (FR-068); **remove** the Infer dropdown → read-only status line; make `classify` **disabled-with-hint**
  when a tenant holds the lease (A1); UI security tests unchanged. Exit: truthful tab, BFF contract intact.
- **Phase 4 — Cross-cutting regression → SC-048**: full 001/006 keyed sweep + the six UI tabs/BFF;
  confirm LLM/training 409/507 codes, SSE frames (byte-identical), tracing, and idle-release timing are
  unchanged; GPU stack still frozen (no torch-family movement).

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Single shared lease over **separate native processes** (serving/training/vision) | The three GPU users are distinct OS processes on the WSL host; a correct mutex must serialize across processes, not just within the gateway | An in-process gateway semaphore (option c alone) does not serialize separate native daemons — the exact cross-process gap that lets the TOCTOU race exist today |
| Atomic acquire (closes TOCTOU) | Two independent HTTP health polls are a check-then-act with a race window; the platform's defining constraint must not have a race | The current two-guard polling "works in practice" but is racy under true concurrency — unacceptable for a NON-NEGOTIABLE principle |
| Live `nvidia-smi` free-VRAM admission | A fragmented/occupied GPU can have less free VRAM than the static file-size estimate assumes; admission must reflect reality | `_fits()`'s file-size×1.2+1 estimate is a guess that can admit a load that won't actually fit |
| Constitution amendment v1.4.0 (generalize Principle II) | Covering vision/ASR + a race-free lease *changes the wording* of the non-negotiable principle; the workflow requires a documented amendment | Silently broadening Principle II in code without ratifying the wording violates the governance rule (any deviation needs a documented amendment) |
| Vision on GPU reusing cu128 torchvision | Proves the lease is modality-general and removes the CPU-only carve-out; the stack is already installed | Keeping vision CPU-only leaves a permanent exception to the (now generalized) rule and never exercises eviction across two real tenants |
| Behavior-preserving LLM/training (subsume, don't rewrite) | The 001/006 contracts (codes, SSE framing, tracing, idle-release) are validated and depended-on | A from-scratch admission rewrite risks regressing the validated serving/training/tracing behavior for no functional gain |
| **Atomic lockfile** lease primitive (GRILLED) | The GPU is held by the three native WSL daemons sharing the WSL filesystem; a PID-stamped `O_CREAT\|O_EXCL` lockfile (stdlib, no dep) serializes them across processes and survives a gateway restart | **Gateway-brokered token** rejected: the Docker gateway only proxies + never holds the GPU, training is fire-and-forget, so a token couples admission to the gateway being up + makes it stateful (lease lost on restart). **In-gateway asyncio semaphore** rejected: can't gate long-running native work across separate OS processes at all |
| **A1 disable-with-hint** for classify-on-busy (GRILLED) | Fits the cooperative refuse-if-held lease exactly — no preemption machinery; the operator frees the GPU to classify | **A2 swap-on-demand** DEFERRED as a fast-follow: it would upgrade the lease to preemptive (unload-now command + swap orchestration + evict/reload thrash), out of 008 scope |
