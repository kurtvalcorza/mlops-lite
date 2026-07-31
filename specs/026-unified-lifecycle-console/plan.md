# Implementation Plan: 026 Unified ML Lifecycle Console

**Branch**: `claude/tech-stack-overview-i0cljp` | **Date**: 2026-07-31 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/026-unified-lifecycle-console/spec.md`

## Summary

Replace the 021 loop-native console with a ten-area operational console that presents one coherent
lifecycle over five systems of record, and **build the read surfaces that make it possible**. The
Phase 0 audit ([research.md](./research.md) R0) found that ten of the spec's display requirements
have no readable API today — per-device GPU topology, engine process detail, admission decisions,
journal reads, trace reads, prediction reads, run listing, endpoint records, capture/label listing,
and search. The interface is the smaller half of this increment.

Approach: **extend existing seams, add no resident service, add no runtime dependency, and expect no
migration.** New reads land as thin routes on the gateway and agent; multi-source joins happen in the
gateway (the only place holding every credential); the console reaches the agent only through the
gateway, preserving the single trust boundary 023 US2 established. Charts are hand-rolled SVG.
Endpoints are a synthesized read model rather than a new table, so 026 needs no schema change.

The truthfulness properties (conflict disclosure, per-service degradation, data age, mode badge) are
treated as **infrastructure written once**, not as per-page polish — they are what makes a
multi-backend console trustworthy, and retrofitting them is far more expensive than building them
into the fetch layer from the start.

## Technical Context

**Language/Version**: TypeScript 5.9 / React 19 / Next.js 15.5 App Router for `ui/`; Python 3.11–3.12
for the gateway, host agent, and `platformlib`.

**Primary Dependencies**: **unchanged — zero additions on both planes.** UI stays exactly `next` +
`react` + `react-dom` with Tailwind 3.4 as tooling (SC-198); charts are hand-rolled SVG (research
R11). The gateway reuses `mlflow-skinny` (already pinned) for trace reads and `psycopg` for
prediction reads. The agent stays stdlib-only apart from its existing `nvidia-ml-py`, whose
multi-device calls (`nvmlDeviceGetCount`, `…GetMemoryInfo`, `…GetUtilizationRates`,
`…GetComputeRunningProcesses`) are already available in the pinned version — no new package.

**Storage**: Postgres `gateway` DB + Garage, both present. **No schema change is anticipated** —
predictions, labels, captures, jobs, policies, and suggestions all already have tables, and endpoints
are synthesized rather than persisted (research R7). Any genuinely-needed change lands as a NEW
numbered `platformlib/migrations/*.sql` (FR-438), verified explicitly rather than assumed.

**Testing**: pytest offline suite (web-free where the logic is web-free) for the new gateway/agent
reads and the joins; the three existing UI suites (`test_ui_smoke.py`, `test_ui_security.py`,
`test_ui_resilience.py`) extended for the new IA, redirects, and degradation matrix; `npm run lint` +
production build under the existing `ui` CI job. GPU-touching legs are `[HW]` (constitution gate
zero).

**Target Platform**: single machine — Compose infra + native WSL agent/UI; console loopback-bound at
`127.0.0.1` behind the existing key-injecting BFF.

**Project Type**: full-stack increment over the existing web-service + native-agent + shared-lib +
Next.js-console monorepo. Front-end rebuild **plus** ten net-new backend read surfaces.

**Performance Goals**: no regression on any serving or training path. Console reads must not add a
per-poll subprocess fork (the 018 regression NVML replaced) and must not open a long-lived stream per
panel against the agent's bounded transport (research R10). Runtime snapshots ride the existing
1-second-TTL cached GPU reader.

**Constraints**: one GPU tenant (Principle II) — the console **reads** admission and never mutates
it; dependency-light (III); console-only Node (workflow amendment); the agent is reachable only via
the gateway (023 US2); the agent transport's 1 MiB JSON cap forces paging on journal and prediction
reads.

**Scale/Scope**: ten net-new read surfaces, ~10 primary areas with ~30 secondary views, a normalized
type layer, a shared fetch/polling layer carrying the truthfulness properties, and six hand-rolled
chart primitives. This is a **large** increment — see Complexity Tracking for the mitigation and the
pre-agreed cut line.

## Constitution Check

*GATE: re-checked after design.*

| Principle | Verdict | Notes |
|---|---|---|
| I. Local-First, Single-Machine | ✅ Pass | No cloud, no cluster. Multi-host is contract shape only (FR-382); no multi-host orchestration is built. |
| II. Single-GPU, On-Demand (NON-NEGOTIABLE) | ✅ Pass | The console is **read-only** with respect to admission. FR-379 forbids any job-preempting control. The per-device snapshot is side-effect free and rides the existing cached reader — it never takes the claim path. Naming honesty enforced: admission **refuses**, so no "queue" is shipped (research R1). `[HW]` validation required (SC-201/202). |
| III. Lightweight Footprint | ✅ Pass | **Zero new runtime dependencies on either plane** (SC-198). No broker, scheduler, analytics store, or serving runtime (FR-434). No new resident service — the new reads are routes on processes that already run. Charts hand-rolled (R11). |
| IV. Full Lifecycle Coverage | ✅ Pass / advances | No lifecycle stage becomes unreachable; the loop survives as the Overview timeline (FR-363). Three previously-invisible domains — runtime, evaluation-as-activity, and the inference record — gain surfaces. |
| V. Open-Source & Swappable | ✅ Pass / advances | The normalized type layer (`PlatformModel`/`PlatformJob`/`PlatformHealth`) is exactly the swappability seam: the interface stops depending on vendor payload shapes. FR-366 preserves tracking vocabulary where it is meaningful, which is compatible with swapping the implementation behind it. |
| VI. Reproducibility & Observability | ✅ Pass / advances | Makes already-tracked state legible. Nothing becomes less tracked. Data age and provenance labelling (FR-381/430) make observability *honest*, not merely present. |
| VII. Incremental, Phase-Gated | ⚠️ **Pass with justification** | MVP 2/3 are already phased out to 027/028. MVP 1 remains ten stories — larger than a typical slice. Every story is independently shippable and ordered so the increment can stop early. See Complexity Tracking. |

**Result**: no violations. Principle VII carries a documented justification rather than a breach, and
Principle II's on-hardware verification is flagged `[HW]`, not skipped.

**Post-design re-check**: unchanged. The Phase 1 design added no dependency, no service, and no
migration; R5 (single trust boundary) and R7 (no endpoints table) each *removed* a candidate
violation that a naive design would have introduced.

## Project Structure

### Documentation (this feature)

```text
specs/026-unified-lifecycle-console/
├── plan.md
├── spec.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── runtime-api.md          # agent: devices, engines(enriched), admission, journal
│   ├── console-read-api.md     # gateway: catalog, jobs, predictions, traces, endpoints, search
│   └── allowlist-delta.md      # the BFF proxy-surface delta, itemized
├── checklists/requirements.md
└── tasks.md                    # /speckit-tasks output — NOT created by /speckit-plan
```

### Source Code (repository root)

```text
hostagent/
├── devices.py            # NEW: per-device snapshot over the cached NVML reader (R2)
├── admission.py          # +bounded decision ring; acquire() records its verdict (R1)
├── journal.py            # +paged/filtered read accessor (R4)
├── lifecycle.py          # +engine process enrichment (pid/vram/identity/requests) (R3)
└── main.py               # +GET /runtime/devices · /runtime/admission · /journal

platformlib/
└── contracts.py          # +DeviceState, AdmissionRecord, JournalEntry; EngineState gains
                          #  OPTIONAL fields only, so 018/019 conformance stays green (R3)

gateway/app/
├── runtime.py            # NEW: agent runtime proxy (X-Agent-Key), the only path to :8100 (R5)
├── console/              # NEW: the multi-source join layer (R8)
│   ├── catalog.py        #   registry × artifacts × deployments × evaluations
│   ├── jobs.py           #   gateway job × agent execution × tracking run + StateConflict (R9)
│   ├── endpoints.py      #   synthesized read model — no table (R7)
│   ├── predictions.py    #   prediction/capture/label reads over existing tables
│   ├── traces.py         #   normalized generic span tree via mlflow-skinny (R6)
│   └── search.py         #   composed resolver (FR-368)
└── routers/console.py    # NEW: the read routes the console consumes

ui/
├── app/(console)/        # NEW: ten areas — overview, models, training, evaluations,
│                         #      deployments, inference, datasets, runtime,
│                         #      observability, administration
├── app/api/gw/           # KEPT + hardened: allowlist, key injection, CSRF/CSP (004/005)
├── lib/
│   ├── gw-allowlist.ts   # re-sectioned to the ten areas + the 026 additions
│   ├── platform-types.ts # NEW: normalized types (FR-433 capability flags)
│   ├── use-live.ts       # NEW: visibility-gated polling, backoff, last-known-good, data age
│   └── charts/           # NEW: six hand-rolled SVG primitives (R11)
└── app/{serving,data,training,models,monitoring,retraining}/  # DELETED → redirects (R12)

tests/
├── test_runtime_api.py          # agent devices/admission/journal, web-free with fakes
├── test_console_joins.py        # join correctness + StateConflict detection (R9)
├── test_console_read_api.py     # gateway read routes + contract conformance
├── test_ui_smoke.py             # EXTENDED: ten areas render
├── test_ui_security.py          # EXTENDED: payload/credential/path-allowlist assertions
├── test_ui_resilience.py        # EXTENDED: the seven-service degradation matrix (FR-428)
└── test_ui_redirects.py         # NEW: every retired 021 path resolves (FR-364/SC-186)
```

**Structure Decision**: extend existing homes. The agent gains one module (`devices.py`) and three
routes; the gateway gains a `console/` join package and one router; `platformlib.contracts` gains
three dataclasses and optional fields on one. The UI is rebuilt in place under a `(console)` route
group (research R12) while the BFF, its allowlist, and the 004/005 security guards are **kept and
hardened** — they are not 021 artifacts and must survive the IA replacement.

## Design Phases

### Phase 0 — Research

Settled in [research.md](./research.md); the load-bearing decisions:

- **R1** Admission refuses rather than queues → ship a decision **record**, never a "queue". Building
  a queue view would be the same fake-semantics error the source addendum warns about for
  orchestration and alert delivery.
- **R2** Per-device topology rides the existing 1s-TTL cached NVML reader; every device carries its
  `source` so FR-381's fallback labelling is data, not inference.
- **R5** The console never reaches the agent directly — one trust boundary, preserved.
- **R7** Endpoints are synthesized, so 026 needs **no migration**.
- **R8** Joins live in the gateway; the BFF stays a security boundary.
- **R9** Conflicts are computed per observation, never persisted.
- **R10** SSE only where it already exists; disciplined polling elsewhere, because ten live panels
  against the agent's bounded transport is a self-inflicted control-plane DoS.
- **R11** Six hand-rolled SVG chart primitives; zero charting dependency.
- **R13/R14** Single operator; the mode badge is resolved from reachability, never self-declared.

### Phase 1 — Contracts and models

- **[data-model.md](./data-model.md)** — the normalized entities and, critically, the **projection
  rules**: which source wins for each field, and what constitutes a conflict rather than a
  precedence. Includes the job-state normalization table (FR-392) and the redirect map (FR-364).
- **[contracts/runtime-api.md](./contracts/runtime-api.md)** — the three new agent routes, their
  paging discipline, and the `EngineState` extension's backward compatibility.
- **[contracts/console-read-api.md](./contracts/console-read-api.md)** — the new gateway read routes.
- **[contracts/allowlist-delta.md](./contracts/allowlist-delta.md)** — the BFF proxy-surface delta,
  itemized, in the 021 tradition of making every added proxy route a deliberate, reviewable entry.
- **No migration expected** (R7). This is *verified* as a task, not assumed — mirroring 025's T595.

### Phase 2 — Tasks

See tasks.md (`/speckit-tasks`). Ordering is deliberate: the **foundational** layer (normalized
types, the live-fetch layer carrying the truthfulness properties, the console shell, the redirects)
comes first because every story depends on it; then US2's backend, which is the largest unknown and
therefore should fail early if it is going to; then the remaining areas, each independently
shippable; then the `[HW]` tail.

## Complexity Tracking

> Principle VII is the one gate carrying a justification.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| MVP 1 spans ten user stories in one increment | The IA replacement is atomic by nature: the loop nav cannot be half-removed, and shipping five of ten areas leaves the console with dead navigation entries. The requester explicitly chose full replacement over an incremental graft. | **Extending the loop nav in place** was offered and declined — it produces a ten-item "loop" that is no longer a loop. **Running both IAs behind a flag** doubles the maintained surface for the increment's length while the 021 views atrophy. |
| Ten net-new backend read surfaces in a console increment | The requester chose full-stack explicitly. Under a UI-only constraint, Runtime, Inference, and Traces render empty — which is the majority of the increment's differentiated value. | **UI-only over existing endpoints** was offered and declined. **Stubbing with "not available"** ships the navigation for a console that cannot answer the questions it is organized around. |

**Mitigation, and the pre-agreed cut line.** Every story is independently shippable and the task
order is dependency-first, so the increment can stop cleanly at any checkpoint. If it overruns, the
cut is **US8 (Datasets) and US9 (Observability/Administration)** — both deepen surfaces that partly
exist today, so cutting them degrades rather than breaks the console — and they phase to 027 ahead
of MVP 2. **US1, US2, US3 and US10 are the irreducible core**: the shell, the runtime console that
justifies the increment, the catalog, and the truthfulness layer without which every other surface
can confidently display a falsehood.

**Residual risk.** US2's `[HW]` legs (SC-201/202) cannot be validated in a container. They ship as
explicit hardware tasks in the 023/025 tradition — flagged, not skipped — and the offline suite pins
everything around them with injected fakes so only the genuinely hardware-dependent behaviour waits
on the RTX 5070 Ti.
