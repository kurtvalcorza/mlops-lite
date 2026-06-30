# Implementation Plan: Swap-on-Demand (preemptive GPU lease for serving)

**Branch**: `017-swap-on-demand` | **Date**: 2026-06-30 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/017-swap-on-demand/spec.md` (DRAFT — GRILLED). 008's deferred A2.

## Summary

Upgrade 008's **cooperative refuse-if-held** GPU lease to **operator-confirmed preemptive swap for serving
tenants**. A serving request carrying an explicit **`preempt=true`** triggers, when a *serving* model is
resident, a **gateway-brokered swap**: the gateway calls an **`unload-now`** endpoint on the holder
(LLM/vision/ASR supervisor), which **drains in-flight requests within a bounded timeout** then unloads +
releases the lease; the gateway waits for the lease to free and forwards the request to the target daemon,
which acquires + loads. The swap is **sequential** (one model in VRAM, Principle II). A running
**training/HPO/batch** holder is **never** preempted (refuse-if-held). The Infer tab's 008/A1 "classify
disabled" becomes a cost-stating **"Swap & classify"** confirm. Default (no `preempt`) behavior is
byte-for-byte 008.

## Technical Context

**Language/Version**: Python 3.12 (gateway + native serving supervisors). No new runtime.

**Primary Dependencies**: Existing only — each supervisor's `_unload` + `serving/gpu_lease.py`
acquire/release/`current_holder`; the gateway's `/serving/state` holder read; FastAPI. **No new
dependency** (Principle III).

**Storage**: None (control-plane feature; no persistence beyond the existing lease file).

**Testing**: `pytest` — unit (the gateway swap orchestration + the supervisor drain/unload, with the lease
+ daemon HTTP mocked) + on-hardware (LLM resident → `preempt=true` classify swaps; training holder is
refused; in-flight drains; `nvidia-smi` one-model-in-VRAM).

**Target Platform**: Win11 + WSL2 + NVIDIA (hybrid-GPU). The `unload-now` endpoints live on the **native
WSL serving supervisors**; orchestration is in the **Docker gateway**.

**Project Type**: Local MLOps platform. Change spans each serving supervisor (`unload-now` + drain), the
gateway (swap orchestration + the `preempt` flag on serving routes), and the UI Infer tab + BFF.

**Performance Goals**: A swap ≈ drain (bounded) + unload + target load (~2.5s per 008's estimate). Default
(non-preempt) path unchanged.

**Constraints**: **One model in VRAM at any instant (Principle II)** — sequential evict→free→load.
**Training never preempted.** Frozen GPU stack untouched. Default behavior byte-for-byte unchanged. Public
repo.

**Scale/Scope**: 3 preemptable serving daemons (LLM/vision/ASR) gain an `unload-now`; one gateway swap
orchestrator; one UI confirm.

## Constitution Check

*GATE: must pass before Phase 0. Re-checked after design.* Constitution **v1.4.0**.

| Principle | Assessment |
|---|---|
| **I. Local-First** | ✅ No cloud; control-plane only. |
| **II. Single-GPU On-Demand (NON-NEGOTIABLE)** | ✅ **One model in VRAM preserved** — the swap is sequential (evict → lease frees → load); never two resident. The lease stays the sole admission point. **Training is never preempted.** ⚠️ **Wording check**: 008's v1.4.0 amendment text described the lease as cooperative with "no swap/evict". 017 adds *operator-confirmed preemptive* swap for serving — Principle II's one-tenant rule is unchanged, but the **plan flags a possible one-line v1.4.x wording update** to note preemption-by-confirm is now allowed for serving (the rule itself does not change). **Not a principle change — a description refresh; confirm with the operator before ratifying.** |
| **III. Lightweight Footprint** | ✅ **No new dependency / service**; reuses each supervisor's `_unload` + the 008 lease. |
| **IV. Full Lifecycle Coverage** | ✅ Improves the serving stage UX; adds no stage. |
| **V. OSS & Swappable** | ✅ Reuses existing components. |
| **VI. Reproducibility & Observability** | ➖ Neutral (control-plane; swap events can be logged). |
| **VII. Incremental, Phase-Gated** | ✅ One increment, verifiable on the reference hardware. |

**Verdict: PASS** — Principle II preserved (one model in VRAM; training protected). The only open item is a
**possible v1.4.x wording refresh** for 008's "no swap/evict" line (a description update, not a rule
change) — **resolve with the operator in Phase 0**; no Complexity Tracking entries.

## Project Structure

### Documentation (this feature)

```text
specs/017-swap-on-demand/
├── spec.md          # grilled spec
├── plan.md          # this file
├── research.md      # Phase 0 — the 5 grilled decisions + the constitution-wording question
├── data-model.md    # Phase 1 — preempt request / unload-now / swap (control entities)
├── quickstart.md    # Phase 1 — on-hardware validation guide
├── contracts/
│   ├── unload-now-endpoint.md   # the per-supervisor control endpoint
│   └── preempt-flag.md          # the preempt flag on serving routes + gateway swap orchestration
└── tasks.md         # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
serving/
├── gpu_lease.py                 # unchanged primitive (acquire/release/current_holder reused)
├── llama/supervisor.py          # US3: add `unload-now` (drain in-flight w/ timeout → _unload → release)
├── whispercpp/supervisor.py     # US3: same `unload-now`
└── bento/service.py (vision)    # US3: same `unload-now` (BentoML vision is a lease tenant since 008)

gateway/app/
├── serving.py / routers/*.py    # US1: the `preempt` flag on serving requests; the gateway swap
│                                #   orchestrator (resolve holder → unload-now → wait-free → forward)
├── platform_health.py / serving state  # holder identity + holder KIND (serving vs training) for US2
└── (a small swap helper)        # US2: refuse if the holder is a training/HPO/batch tenant

ui/
├── components/infer/...         # US1/FR-160: A1 "disabled" → cost-stating "Swap & classify" confirm
└── lib/gw-allowlist.ts          # pass the preempt flag through the BFF

tests/
├── test_swap_orchestration.py   # NEW — gateway swap: resolve→unload→wait→forward (mocked daemons/lease)
├── test_no_preempt_training.py  # NEW — training holder + preempt=true ⇒ refused (US2)
└── test_unload_now_drain.py     # NEW — supervisor unload-now drains in-flight then releases (US3)
```

**Structure Decision**: Single-project. Each serving supervisor gains an `unload-now` (reusing its
`_unload`); the gateway gains the `preempt` flag + a swap orchestrator that refuses training holders; the
UI Infer tab swaps its A1 disable for a confirm. No new service/daemon; the lease primitive is unchanged.

## Complexity Tracking

> No Constitution Check violations. The only open item is a **possible v1.4.x wording refresh** (008's "no
> swap/evict" line) — a description update resolved in Phase 0, not a rule change.

## Phase 0 — Research (see research.md)

The 5 grilled decisions + the **constitution-wording question** (does 008's "no swap/evict" text need a
one-line v1.4.x refresh — operator confirms). Open impl items: the drain-timeout default + the supervisor's
"in-flight" detection (request counter vs probe); whether `unload-now` is auth'd like other control routes;
the gateway wait-for-free mechanism (poll `current_holder()` vs a short lease-free wait).

## Phase 1 — Design & Contracts

- **data-model.md**: the control entities — preempt request, unload-now command, swap.
- **contracts/**: `unload-now-endpoint.md` (per-supervisor: drain→unload→release shape) and
  `preempt-flag.md` (the `preempt` flag on serving routes + the gateway swap orchestration + the
  training-refusal behavior).
- **quickstart.md**: LLM resident → `preempt=true` classify swaps; training holder is refused; in-flight
  drains; `nvidia-smi` one-model; default (no preempt) == 008.

## Phase 2 — Tasks (/speckit-tasks)

Generated separately. Expected: (US1) `unload-now` on the three supervisors + the gateway swap orchestrator
+ the `preempt` flag + the UI confirm; (US2) holder-kind detection + training refusal; (US3) drain-with-
timeout; plus the no-regression sweep (default==008) and the dual-bot loop. **Independent of 015/016.**
