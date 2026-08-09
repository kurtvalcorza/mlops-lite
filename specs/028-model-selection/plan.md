# Implementation Plan: 028 Model-Selective Serving

**Branch**: `028-model-selection` | **Date**: 2026-08-09 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/028-model-selection/spec.md`

## Summary

026 built a model-keyed admission core and wired a modality-keyed surface on top of it. The
`Coordinator` keys `residents`, `_generation`, `_claims`, and `_select_victims` on a `model_key`
(`hostagent/coordinator.py`); every caller above it passes an `engine_id` into that slot. The
OpenAI router never reads `body.model` for routing at all — it posts to
`settings.SERVING_URL = f"{AGENT_URL}/engines/llm"`, a modality slot.

The plan therefore has an unusual shape for a P1 fix: **the state machine is not touched.** The work
is three caller-side joins and one new bound —

1. **Resolve** `model` → `(name, version, modality)` in the gateway, against the registry, before any
   agent call (FR-439–FR-445).
2. **Address** the request to that model on the wire, and have the agent route it to the child hosting
   that `model_key` rather than to whatever occupies the engine (FR-446–FR-450).
3. **Place** a promoted, non-resident model through `Coordinator.admit_serving` with a real per-model
   estimate, and report the placement truthfully (FR-451–FR-455, FR-458–FR-466).
4. **Bound** eviction thrash with a minimum-residency window, the one genuinely new mechanism
   (FR-456–FR-456c).

Delivery is phase-gated per Principle VII, and the gate is chosen so that **the P1 finding closes in
Phase 1** — before any GPU behavior changes at all. Phase 1 is gateway-only: resolution, truthful
responses, truthful listing, and refusal for anything not currently resident. That alone ends the
silent substitution, which is the defect. Phases 2 and 3 turn refusals into service.

## Technical Context

**Language/Version**: Python 3.12 (gateway, host agent, `platformlib`); TypeScript 5 / React 18 on
Next.js for the console surfaces that display residency. No new language.

**Primary Dependencies**: Existing only. The host agent (`hostagent/` — `coordinator.py`,
`coordadmission.py`, `lifecycle.py`, `admissionlog.py`) is **stdlib-only** and must stay so; the
FastAPI gateway (`gateway/app`) owns MLflow access via `gateway/app/registry.py`. This asymmetry is
load-bearing for the design: **the agent cannot resolve a model name, because it cannot reach the
registry.** Resolution is therefore a gateway responsibility and the resolved identity crosses the
wire.

**Storage**: No schema change. The admission journal (`hostagent/admissionlog.py`) already records
`model_key` per decision; it starts carrying a model rather than an engine id.

**Testing**: `pytest`. New coverage in `tests/test_broker_openai*.py` (resolution and refusal
mapping), `tests/test_agent_coordinator.py` (minimum-residency window and victim eligibility),
`tests/test_broker_coadmission.py` (model-keyed shim), plus contract tests for the revised
`/v1/models` and the model-addressed inference call.

**Target Platform**: single local machine — Docker Compose infra + native WSL GPU host agent.

**Project Type**: web platform (multi-package repo). No new package.

**Performance Goals**: resolution adds a registry lookup to the request path; it must be cached with
a bounded TTL so a chat request does not pay an MLflow round-trip per call. Target: **≤ 5 ms added at
p50 on a cache hit** — the common case, a resident model requested repeatedly. Measured by T781a, not
assumed; an unmeasured performance goal is a wish, and the earlier "a few ms" wording gave nothing to
check (`/speckit-analyze` finding A2).

**Constraints**: Principle II (v1.6.1) — accounted residents plus reservations within
`usable_capacity`, and each incoming load within live free VRAM less `safety_headroom`. Exclusive
jobs take the whole GPU and are never preempted. The agent stays stdlib-only. The agent's route
allowlist is asserted by `tests/test_console_allowlist.py` and by 024's
`contracts/preservation.md` — a new top-level agent route is a contract change, not a detail.

**Scale/Scope**: one GPU; a small model zoo bounded by disk (Principle III); a handful of LAN
tenants. Three phases, of which Phase 1 closes the reported defect.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment |
|---|---|
| **I. Local-First, Single-Machine** | PASS. No new service, no cloud dependency, no new runtime. |
| **II. Single-GPU, On-Demand Serving (NON-NEGOTIABLE)** | PASS, **no amendment required.** v1.6.1 already admits bounded co-residency of serving tenants within the usable budget and already *requires* eviction by "idle-first / least-recently-used ... or refuse with a clear reason if it cannot fit even alone". 028 implements that clause for LLMs rather than widening it. Training exclusivity and never-preempt are untouched (FR-455). The minimum-residency window (FR-456) makes eviction *strictly rarer* than the constitution permits, so it cannot breach the principle. |
| **III. Lightweight Footprint** | PASS. Multiple resident LLM children raise resident host RAM as well as VRAM, and the coordinator bounds only VRAM. **FR-467 makes the host-RAM bound an admission precondition** and FR-468 requires it calibrated against a measurement; SC-211 is its criterion. An earlier draft of this plan asserted the obligation without the spec carrying a requirement for it — `/speckit-analyze` finding D1. |
| **IV. Full Lifecycle Coverage** | PASS. Strengthens the serving stage's link to the registry stage. |
| **V. Open-Source & Swappable** | PASS. No component replaced. |
| **VI. Reproducibility & Observability** | **Strengthened.** Today a prediction's recorded model is the one that answered while the tenant's record says what it asked for; FR-458–FR-460 make those the same fact, and FR-463 journals every placement with the resolved identity. |
| **VII. Incremental, Phase-Gated Delivery** | PASS. Three phases, each independently runnable and independently valuable; Phases 2 and 3 require hardware validation before they are called done. |

**Gate result: PASS with no violations.** The Complexity Tracking table below is therefore empty, and
`.specify/memory/constitution.md` is not modified by this increment.

One governance note that is *not* a violation but must not be lost: FR-457 (version qualifiers must
match the promoted version) is what keeps 022's gated promotion the sole path by which a version
becomes servable. If a later revision relaxes FR-457, that relaxation — not this plan — is what
would need to argue against 011's evaluation gate.

## Project Structure

### Documentation (this feature)

```text
specs/028-model-selection/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── model-resolution.md      # request string → (name, version, modality); refusal codes
│   ├── model-addressed-infer.md # the gateway↔agent wire change
│   └── residency-window.md      # minimum-residency semantics and Retry-After derivation
├── checklists/
│   └── requirements.md  # written by /speckit-specify
└── tasks.md             # /speckit-tasks output — NOT created here
```

### Source Code (repository root)

```text
gateway/app/
├── routers/
│   └── broker_openai.py      # resolve `model`; address the agent by model; truthful echo (P1)
├── registry.py               # add modality to the listing; resolve name[:version] → identity
├── modelresolve.py           # NEW — resolution + cache + refusal taxonomy (P1)
└── settings.py               # SERVING_URL stops being the sole serving address

hostagent/                    # stdlib-only
├── coordinator.py            # + became_resident_at; victim eligibility honours the window (P2)
├── gpuconfig.py              # + min_residency_s tunable (P2)
├── coordadmission.py         # stop collapsing model_key → engine_id on the serving path (P2)
├── lifecycle.py              # one runtime per resident model, not per engine (P3)
└── main.py                   # model-addressed dispatch on the existing engine verb route (P2)

tests/
├── test_broker_openai_resolution.py   # NEW (P1)
├── test_agent_coordinator.py          # + residency-window cases (P2)
├── test_broker_coadmission.py         # + model-keyed shim cases (P2)
└── test_console_allowlist.py          # unchanged — asserts no new agent route appears
```

**Structure Decision**: No new package and no new top-level agent route. Resolution is a new gateway
module (`gateway/app/modelresolve.py`) because it is registry-facing and the agent cannot be; the
agent-side changes are edits to existing files. The wire change rides the **existing** engine verb
route as a request field rather than a new resource path, specifically so
`tests/test_console_allowlist.py` and 024's preservation contract keep passing without amendment —
that test exists to catch exactly the kind of quiet surface growth this increment could otherwise
introduce.

## Phasing

Each phase is independently runnable and independently valuable, per Principle VII.

**Phase 1 — Resolution and truth (gateway only; closes the P1 finding).**
`model` is resolved against the registry and validated. A request for the currently resident model is
served as today. A request for anything else is refused with the correct code — unknown, unpromoted,
wrong-modality, or `gpu_busy`. Responses and `/v1/models` report resolved identities.
**No GPU behavior changes**, no agent change, and the coordinator is not touched. Shipping only this
means the platform never again serves model B to a request for model A. Verifiable without a GPU.

**Phase 2 — Model-keyed admission and on-demand placement (agent; hardware-gated).**
`coordadmission.py` stops collapsing `model_key` to `engine_id` on the serving path. The
minimum-residency window lands in the coordinator. A promoted, non-resident model is admitted through
`admit_serving`, evicting idle/LRU residents outside their window when it must. The LLM engine still
hosts one model at a time, so a second LLM is a swap rather than a co-residency — but the accounting,
the journal, and the refusal reasons are all model-truthful. Requires GPU validation.

**Phase 3 — LLM co-residency (agent; hardware-gated).**
`lifecycle.py` gains one runtime per resident model rather than one per engine, so two LLMs that both
fit are both resident and neither request evicts the other. This is the last unimplemented sentence
of 026's inference contract. Requires GPU validation and a host-RAM bound (Principle III).

The `BROKER_COORDINATOR_ADMISSION` flag remains the operational gate for Phases 2 and 3. Phase 1's
behavior must be correct in **both** flag positions, and that is a test obligation, not an assumption.

## Constitution Re-Check (post-Phase 1 design)

Re-evaluated against the design artifacts now that they exist. **Still PASS, with two things the
design made concrete rather than assumed:**

- **Principle II** — the design *reduces* eviction frequency relative to what v1.6.1 permits
  (`contracts/residency-window.md`), and FR-456c keeps the window off the exclusive-job path
  entirely. `min_residency_s = 0` reproduces 026 exactly, which is what makes the characterization
  suite a real check rather than a rewritten one.
- **Principle III** — now a *requirement* (FR-467) rather than a plan-level caveat: the host-RAM
  bound is an admission precondition, refused transiently rather than admitted and reconciled, because
  host RAM cannot be reclaimed by the coordinator once a child has allocated it. FR-468 requires the
  bound calibrated against a measurement (`quickstart.md`, "Also measure host RAM"). Two resident LLM
  children is the first time this has mattered.

One design decision worth re-stating because it is what keeps the constitution check cheap: the
admission state machine is **not** modified beyond one field, one config value, and a richer return
shape from `_select_victims`. Everything that would have required an amendment — co-residency,
eviction policy, the two VRAM bounds — 026 already ratified and built.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No violations. No amendment, no new runtime, no new package, no new agent route, and no rebuild of
the admission state machine.
