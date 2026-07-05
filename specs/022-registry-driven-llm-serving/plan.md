# Implementation Plan: Registry-Driven LLM Serving & Operator Model Selection

**Branch**: `022-registry-driven-llm-serving` | **Date**: 2026-07-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/022-registry-driven-llm-serving/spec.md`

## Summary

Make the text-generation (LLM) engine a **registry-driven, console-operable** engine like the
others. Today every other serving child (vision, embeddings, ASR, tabular) is pinned to a model
**name** and resolves that name's `@serving` **version's artifact from the MLflow registry on each
cold load** (`serving/children/tabular_service.py`, `embed_service.py`) — so promote + reload serves
the new version. The `llama.cpp` LLM adapter is the **sole outlier**: it loads a fixed GGUF **file
path** from an environment variable (`hostagent/adapters/llama.py`) and never consults the registry,
so promoting an LLM in the console does nothing, fine-tunes aren't discoverable, and the served
identity diverges from what actually runs.

Technical approach — adopt the existing cold-load resolution pattern for the LLM and extend it to
**base + LoRA adapter**, plus honest identity reporting:

1. **Serving-LLM selection** — a platform-scoped pointer to the active text-generation model
   (spanning model *names*, since operators switch between distinct LLMs like a base vs a fine-tune),
   set through the existing gated promote from the console. (research R1)
2. **Registry-resolved artifact at cold load** — the `llama.cpp` adapter resolves the active
   serving-LLM's `@serving` version → its artifact set (a full GGUF, **or** an adapter GGUF + its
   registered base GGUF via lineage) before spawning `llama-server`, generalizing the `LORA` env
   prototype (commit `c28ca97`) to a registry lookup. (R2/R3)
3. **Reload-on-promote under the lease** — promoting/selecting the serving LLM triggers a controlled
   agent reload (evict → load) through the existing admission/swap path; operator-confirmed when it
   preempts a resident serving model; a running job is never preempted. (R4)
4. **Honest served identity** — the agent reports the actually-loaded model name + registry version
   in health; the gateway's serving-state and prediction logging consume the **agent-reported**
   identity instead of a fixed config string, so monitoring windows key on the right model+version.
   (R5)
5. **Discoverability** — the fine-tune flow stamps `task=text-generation` + serving-engine + base
   lineage at registration, and a one-time backfill fixes already-registered untagged LLM versions
   (e.g. `ops-bot-v1/v2`). (R6)
6. **Console surface** — the Models/serving stage shows a text-generation version's base-vs-adapter
   nature + lineage, the resident-vs-promoted delta, and triggers the switch. (021 surfaces extended)

This is a **backend + frontend** feature (unlike 021), owning the engine resolution, the registry
serving-LLM pointer + descriptors, the gateway identity reporting, and the console surface.

## Technical Context

**Language/Version**: Python 3.12 (gateway, host agent, training flows, serving children); TypeScript
5 / React 18 on Next.js (operator console). No new language.

**Primary Dependencies**: Existing only — MLflow (registry + `@serving` alias), llama.cpp
(`llama-server`, `--lora`), the GPU host agent (`hostagent/` admission + lifecycle + swap),
`platformlib` (store/topology), the Next.js console + key-injecting BFF. **No new runtime
dependency.**

**Storage**: MLflow model registry (versions, `@serving` aliases, tags/lineage) + the relational
store (018 US4, Postgres) for the platform-scoped active-serving-LLM selection + honest prediction
identity. Model artifacts on Garage (S3) + local GGUF zoo. No new store.

**Testing**: `pytest` for the backend (resolver, identity reporting, promote→reload, backfill) in
the existing suite; `next lint` + `next build` (type-check) for the console (021 posture, no UI
unit/e2e harness); browser + on-hardware quickstart drills for the live swap/serve/identity.

**Target Platform**: single local machine — Docker Compose infra + native WSL GPU host agent;
console on `127.0.0.1:3000` through the BFF. Principle I — nothing leaves the machine; bases are
resolved from the local zoo, never auto-downloaded.

**Project Type**: web platform — FastAPI gateway + native GPU host agent + Next.js console in the
existing multi-package repo. No new top-level package.

**Performance Goals**: switching the served LLM is a single sequential reload (evict → load), on the
order of a cold model load (seconds); the console reflects the new served identity within the live
`platform/events` cadence (~2 s). No added per-request latency on the steady-state inference path.

**Constraints**: Principle II (one model in VRAM — the switch is a controlled sequential reload,
never two-resident, jobs never preempted); `127.0.0.1` only; every gateway call through the
allow-listed BFF; byte-compatible `/infer` + `/infer/stream` response contracts preserved; bases
bounded by the local zoo (Principle III); no new resident process.

**Scale/Scope**: one operator; one active serving LLM at a time (Principle II); a handful of
text-generation models (bases + fine-tunes) in the registry. Requirement IDs FR-254..274,
SC-143..151; tasks from T461.

## Constitution Check

*GATE: evaluated against constitution v1.5.1 — PASS (no violations).*

- **I. Local-First**: PASS — registry resolution is local MLflow; base + adapter GGUFs are resolved
  from the local zoo (no auto-download; absent base → promotion refused, FR-265). Nothing added
  leaves the machine.
- **II. Single-GPU lease (NON-NEGOTIABLE)**: PASS — switching the served LLM is a **controlled
  sequential reload** (evict → free → load) through the existing host-agent admission/swap
  (`hostagent/swap.py`, `admission.py`); at no instant are two models resident (FR-257/SC-147). A
  displacement of a resident serving model is operator-confirmed (FR-258, the 021 ConfirmDialog); a
  running training/HPO/batch job is **never** preempted (FR-259/SC-150, existing refuse). This
  feature selects *which* one model serves — it never relaxes the one-tenant rule.
- **III. Lightweight Footprint**: PASS — no new resident service; resolution happens on cold load
  (no new daemon); bases are already in the zoo; the only new persisted state is a single
  active-serving-LLM pointer + honest prediction identity in the existing store. Net resident
  processes unchanged.
- **IV. Full Lifecycle Coverage**: PASS — strengthens Principle IV: it makes the serving↔registry
  link coherent for the LLM (train → register → **promote → serve** → monitor now closes for the
  LLM as it already does for the other modalities).
- **V. Open-Source & Swappable**: PASS — no stack/tool change; llama.cpp + MLflow unchanged; the LLM
  adopts the same registry-resolution the vision/embed/tabular children already use.
- **VI. Reproducibility & Observability**: PASS — records + surfaces the served LLM's identity and
  (base, adapter, version, lineage) provenance (FR-274), and fixes the mis-attributed prediction
  logging that corrupted the monitoring window — strictly *more* observable/reproducible.
- **VII. Phase-Gated**: PASS — 5 independently-shippable user stories (US1/US2 P1 correctness-MVP;
  US3/US4 P2 fine-tune usability; US5 P3 contended-switch hardening), each a standalone slice.

*Post-design re-check (after Phase 1)*: PASS — no design artifact introduces a new resident service,
a second GPU tenant, an admission-path change, or a cloud dependency. The switch is the existing
swap; the resolver is cold-load-only.

## Project Structure

### Documentation (this feature)

```text
specs/022-registry-driven-llm-serving/
├── spec.md              # /speckit-specify output (done)
├── plan.md              # This file
├── research.md          # Phase 0 (R1–R8)
├── data-model.md        # Phase 1 — serving-LLM selection, LLM version kinds, served identity
├── quickstart.md        # Phase 1 — per-US validation drills (backend + browser + [HW])
├── contracts/
│   ├── serving-resolution.md   # the registry serving-LLM pointer + base/adapter resolution contract
│   └── agent-identity-and-allowlist.md  # agent honest-identity health fields + BFF allow-list delta
├── checklists/
│   └── requirements.md  # spec quality checklist (done)
└── tasks.md             # /speckit-tasks output (NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
hostagent/
├── adapters/llama.py            # RESOLVE base+adapter from the registry at spawn (generalize the
│                                #   c28ca97 LORA env prototype → registry lookup); report identity
├── serving_llm.py               # NEW: resolve the active serving-LLM → {base_gguf, adapter_gguf,
│                                #   model_name, version} from the registry (mirrors children's
│                                #   cold-load @serving resolution, extended to base+adapter)
├── lifecycle.py / swap.py       # reload-on-select hook: evict → load the resolved target under
│                                #   admission (reuse preempt_for); a control verb to reload `llm`
gateway/app/
├── registry.py                  # active-serving-LLM pointer get/set; task-tag + base resolution in
│                                #   resolve_serving_target; expose a version's base/adapter nature
├── serving.py                   # gpu_state reports the AGENT-reported model_name + registry_version
├── routers/infer.py, stream.py  # log predictions against the agent-reported served identity
├── routers/models.py (or a serving-control router)  # "set serving LLM" → trigger the agent reload
training/flows/finetune.py       # stamp task=text-generation + serving_engine + base lineage at reg.
scripts/
├── backfill_llm_task_tags.py    # NEW: backfill task/engine tags on legacy LLM versions (ops-bot-*)
├── register_base_gguf.py        # NEW: register local base GGUFs as full-model text-generation vers.
platformlib/store                # active-serving-LLM selection persistence (+ served identity)
ui/
├── components/models/*, serving/*  # LLM base-vs-adapter + lineage display; resident-vs-promoted
│                                #   delta; "set serving / switch" trigger with the ConfirmDialog swap
└── lib/gw-allowlist.ts          # allow-list delta for any new serving-control endpoint (auditable)
```

**Structure Decision**: no new top-level package. The load-bearing change is a **new cold-load
resolver** (`hostagent/serving_llm.py`) that the `llama.cpp` adapter calls before spawn to bind its
base + adapter GGUFs from the registry's active serving-LLM — mirroring the vision/embed/tabular
children's existing "resolve `@serving` version each cold load" pattern, extended to the
base+adapter case the LLM needs. The gateway stops reporting a fixed `SERVING_MODEL` and instead
reports the **agent's** actually-loaded identity everywhere serving is named or logged. The console
extends the 021 Models promote-gate + serving stage to make the LLM selectable and to trigger the
controlled reload. Every reachable gateway call remains on the BFF allow-list; the delta (if any) is
explicit (contracts/agent-identity-and-allowlist.md).

## Complexity Tracking

> No Constitution Check violations to justify.

One scope note: this feature deliberately **relaxes the "adapter artifact is frozen for the process
lifetime" property** for the LLM engine only — the `llama.cpp` adapter rebinds its base+adapter GGUF
from the registry on each cold load (it does **not** hot-swap weights in a running process; the
change takes effect on the next controlled reload, exactly like the other children pick up a
promotion after an unload/reload). This is the same late-cold-load-binding the vision/embed/tabular
children already do, not a new hot-reload mechanism, so it introduces no new concurrency surface
beyond the existing admission-guarded reload. Recorded here so the property change is a documented
decision, not an oversight.
