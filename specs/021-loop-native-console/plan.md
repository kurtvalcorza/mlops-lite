# Implementation Plan: 021 Loop-Native Operator Console

**Branch**: `claude/mlops-lite-code-review-vnfhvq` | **Date**: 2026-07-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/021-loop-native-console/spec.md`

## Summary

Rebuild the operator console's information architecture so the navigation **is** the MLOps lifecycle
loop — `data → training → models → serving → monitoring → retraining ⟲` — with per-stage live status
glyphs, a persistent off-axis GPU-lease pill, and an enriched `health` entry. Deepen each stage to
expose the capabilities its backing endpoints already support: the near-invisible monitoring
read-side (drift/quality history + labels), the entirely-absent autonomous retraining layer
(policies + suggestions + cycle board), the promote gate as the models centerpiece, and serving as a
multi-engine surface under one GPU lease.

Technical approach: a **front-end-only** change inside the existing `ui/` Next.js package. Reuse the
already-present dynamic per-task panel machinery (`ui/components/infer/*`), the allow-listed BFF
proxy (`ui/app/api/gw/[...path]`), the `Panel`/`Badge` primitives, and the SSE helper
(`ui/lib/sse.ts`). Rename/relocate the stage routes to the loop vocabulary, add two new stage views
(`retraining`, and `serving` absorbing batch), add shell chrome (loop nav + GPU pill + stage
badges), and extend the proxy allow-list (`ui/lib/gw-allowlist.ts`) to the ~12 already-existing
endpoints the new read/label surfaces need. **No gateway/backend/API changes.** Principle II is
visualize-only: the sole lease-affecting action is the already-sanctioned operator-confirmed
preemptive swap, gated behind a confirmation.

## Technical Context

**Language/Version**: TypeScript 5 / React 18 on Next.js (app router), built by Node. No backend
language touched.

**Primary Dependencies**: Existing UI stack only — Next.js, React, Tailwind, the in-repo
`lib/gw.ts` (typed proxy client), `lib/sse.ts` (EventSource helper), `lib/gw-allowlist.ts` (BFF
allow-list), and the `Panel`/`Badge` primitives. **No new runtime dependencies planned** (self-hosted
JetBrains Mono via `next/font` is already in place).

**Storage**: N/A — the console is stateless; all state is read from the gateway through the BFF
proxy. No client persistence beyond ephemeral view state (and cross-stage hand-off params passed via
URL query).

**Testing**: `next lint` + `next build` (type-check) as the automated gate — the `ui/` package has
**no unit/e2e harness today** (only `lint`). Behavioural validation is the browser-driven quickstart
drills. The platform's Python backend test suite must remain **green and unchanged** (SC-141) since
021 touches no backend code.

**Target Platform**: local browser against the native Next server at `127.0.0.1:3000`
(`next dev`/`next start -H 127.0.0.1`), proxying to the gateway. Principle I — nothing leaves the
machine.

**Project Type**: web frontend — a single Next.js app in the `ui/` package within the existing
multi-package repo. No new top-level package.

**Performance Goals**: perceived-instant navigation between stages; live nav badges + GPU pill
reflect a platform state change within a couple of seconds (SSE `platform/events` + light polling);
the shell renders and navigates even when the platform is unreachable (glyphs degrade, never block).

**Constraints**: `127.0.0.1` only; every gateway call through the allow-listed BFF proxy with the
operator key injected server-side; **no gateway/backend/API changes**; monospace design language and
existing primitives preserved (IA change, not a reskin); Principle II untouched (visualize-only).

**Scale/Scope**: 6 ordered loop stages + 2 off-axis surfaces (GPU pill, health); 5 serving engine
panels + batch; ~12 allow-list additions; single operator. Requirement IDs FR-208..253, SC-134..142;
tasks from T420.

## Constitution Check

*GATE: evaluated against constitution v1.5.1 — PASS (no violations).*

- **I. Local-First**: PASS — the console is a local Next server on `127.0.0.1`, every gateway call
  routes through the in-repo BFF proxy with the operator key server-side; nothing added leaves the
  machine.
- **II. Single-GPU lease (NON-NEGOTIABLE)**: PASS — 021 is **visualize-only**. It surfaces lease
  state (GPU pill + serving lease view) and offers exactly one lease-affecting action — the
  operator-confirmed preemptive swap that Principle II already sanctions — gated behind a
  confirmation naming the holder. It never alters admission, lifecycle, or swap semantics; a running
  training/HPO/batch job is never presented as preemptable.
- **III. Lightweight Footprint**: PASS — no new resident service and no new runtime dependency; the
  UI is a build-time Next app already in the stack. Net resident processes unchanged.
- **IV. Full Lifecycle Coverage**: PASS — this feature *strengthens* Principle IV: it makes the full
  loop (including the previously-invisible gate, monitoring read-side, and close-the-loop automation)
  legible and operable from one surface.
- **V. Open-Source & Swappable**: PASS — no stack/tool change; the serving-library-neutral
  `components/serving/` naming continues the 020 `serving/children` framing.
- **VI. Reproducibility & Observability**: PASS — surfaces *more* observability (lineage drill-back,
  per-model cycle board, prediction-id traceability) without changing what is tracked.
- **VII. Phase-Gated**: PASS — 7 independently-shippable user stories, each a standalone slice
  (US1/US2 are P1 MVP; US3–US7 layer on).

*Post-design re-check (after Phase 1)*: PASS — no design artifact introduces a backend change, a new
resident service, or an admission-path change.

## Project Structure

### Documentation (this feature)

```text
specs/021-loop-native-console/
├── spec.md              # /speckit-specify output (done)
├── plan.md              # This file
├── research.md          # Phase 0 (R1–R8)
├── data-model.md        # Phase 1 — UI-facing view models (read models over existing endpoints)
├── quickstart.md        # Phase 1 — per-US browser validation drills
├── contracts/
│   ├── allowlist-delta.md   # the ~12 BFF proxy allow-list additions + re-sectioning
│   └── nav-and-routes.md     # loop-nav contract: stage order, routes, badges, chrome, hand-offs
├── checklists/
│   └── requirements.md  # spec quality checklist (done)
└── tasks.md             # /speckit-tasks output (NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
ui/                                  # the only package touched (front-end)
├── app/
│   ├── page.tsx                     # landing redirect: /infer → /serving (FR-212)
│   ├── layout.tsx                   # mounts the loop nav + GPU pill in the shell chrome
│   ├── data/page.tsx                # RENAME from app/datasets/ (step 1): + inspect/download + validate-as-gate + "train on this version"
│   ├── training/page.tsx            # RENAME from app/runs/ (step 2): fixed-modality picker, lease-aware launch, → models hand-off
│   ├── models/page.tsx              # (steps 3-4): champion pointer, lineage drill-back, evaluate button, preview→promote gate, override-with-reason
│   ├── serving/page.tsx             # RENAME from app/infer/ (step 5): dynamic all-engine panels + batch + full lease view + confirm-gated preempt + label-this-prediction hand-off
│   ├── monitoring/page.tsx          # RENAME from app/monitor/ (step 6): drift+quality checks + HISTORY reads + labels panel + one-shot retrain + cooldown-as-outcome
│   ├── retraining/page.tsx          # NEW (steps 7-8): policy form+JSON editor, warned auto-promote, cycle board, suggestions inbox, blocked-accept → models override
│   ├── health/page.tsx              # enriched: platform liveness + per-engine probe dots
│   └── api/gw/[...path]/route.ts    # BFF proxy — UNCHANGED (enforces the allow-list below)
├── components/
│   ├── Nav.tsx                      # REBUILD → loop bar (ordered stages + connectors + loop-back + off-axis health)
│   ├── LoopNav.tsx / StageBadge.tsx # NEW: loop-ordered nav + per-stage live status glyph (FR-208/210)
│   ├── GpuPill.tsx                  # NEW: header lease pill (holder + resident + swap state → serving) (FR-211)
│   ├── ConfirmDialog.tsx            # NEW: shared high-trust friction (override reason, preempt, auto-promote) (FR-250)
│   ├── serving/                     # RENAME from components/infer/ (dynamic per-task panels: Stream/Classify/Tabular/Transcribe/Embed/NoRenderer + index/types) + BatchPanel + LeaseView
│   ├── models/                      # NEW: LineageLinks, PromoteGate (preview→promote+override), EvaluatePanel (default+advanced)
│   ├── monitoring/                  # NEW: DriftPanel, QualityPanel, HistoryList, LabelsPanel, OneShotRetrain
│   ├── retraining/                  # NEW: PolicyEditor (form+JSON toggle), CycleBoard, SuggestionsInbox
│   ├── Badge.tsx / Panel.tsx        # existing primitives — reused
└── lib/
    ├── gw-allowlist.ts              # EXTEND: ~12 additions + re-section policy/suggestion entries under retraining (contracts/allowlist-delta.md)
    ├── gw.ts                        # existing typed proxy client — reused (maybe minor typed helpers)
    └── sse.ts                       # existing EventSource helper — reused for badges/lease/live logs
```

**Structure Decision**: no new top-level package; all work is inside `ui/`. The stage routes are
renamed to the loop vocabulary (`datasets→data`, `runs→training`, `infer→serving`,
`monitor→monitoring`) with `retraining` added; the existing `components/infer/*` dynamic-panel set is
relocated to `components/serving/*` (serving-library-neutral, mirroring 020's `serving/children`
rename). The BFF proxy route and its allow-list remain the **single source of truth** for reachable
gateway calls — adding a view means adding its call to `gw-allowlist.ts` on purpose
(contracts/allowlist-delta.md). No call reaches the gateway that is not allow-listed (FR-251).

## Complexity Tracking

> No Constitution Check violations. One scope note: the `ui/` package has no automated
> unit/e2e test harness today (only `next lint`). 021 does **not** introduce one — the automated gate
> stays `lint` + `build` (type-check), and behavioural coverage is the browser-driven quickstart
> drills. Standing up a UI test harness is deliberately out of scope (it would be its own feature)
> and is recorded here so the absence is a documented decision, not an oversight.
