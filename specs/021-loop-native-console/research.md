# Research: 021 Loop-Native Operator Console

Decisions R1–R8. The information architecture was resolved interactively before drafting (the spec's
Input block is the decision record); this file captures the **technical** resolutions behind the
front-end rebuild. No NEEDS CLARIFICATION markers remain.

## R1. Nav-as-loop rendering

- **Decision**: Rebuild `Nav.tsx` into a `LoopNav` that renders the six stages in loop order with
  directional connectors and a loop-back marker returning to `data`; `health` and the GPU pill sit
  off the loop axis (right-aligned). Each stage is a `StageBadge` carrying a live status glyph.
- **Rationale**: the navigation and the mental model become one object — the operator reads the
  lifecycle from the chrome on every page rather than reconstructing it from a noun list. Keeps the
  existing top-nav footprint; the labels are short enough to fit at the current `max-w-[1100px]`.
- **Alternatives considered**: a separate heavy "loop overview" home page (rejected — the live nav
  badges make it redundant; FR-212 lands on `serving` instead); keeping the flat nav and adding a
  loop diagram elsewhere (rejected — leaves the noun-soup nav as the primary surface).
- **Responsive note**: at narrow widths the off-axis GPU pill wraps below the loop bar; the six
  ordered stages stay on one axis so the loop is never broken visually.

## R2. Live status glyphs — data source

- **Decision**: Drive per-stage badges and the GPU pill from the existing SSE stream
  (`platform/events`, already allow-listed, consumed via `lib/sse.ts`) plus light per-stage polling
  for values the event stream does not push. Badge signals: training = active-run indicator, models =
  candidate-awaiting-promotion, serving = resident engine (from `serving/state`), monitoring = breach
  indicator, retraining = open-suggestion count (from `suggestions?state=open`).
- **Rationale**: `platform/events` already exists to broadcast live state; reusing it avoids a poll
  storm. Light polling covers counts the stream does not carry. Both degrade to an unknown/at-rest
  glyph when the platform is unreachable (FR-213), so the shell never blocks.
- **Alternatives considered**: pure polling every stage on an interval (rejected — heavier, laggier);
  a new aggregated "loop status" endpoint (rejected — that is a backend change, out of scope).

## R3. Dynamic serving panels — reuse, don't rebuild

- **Decision**: Relocate `ui/components/infer/*` (StreamPanel, ClassifyPanel, TabularPanel,
  TranscribePanel, EmbedPanel, NoRenderer, index, types) to `ui/components/serving/*` and drive them
  from `GET /serving/tasks` exactly as today — one panel per promoted task, `NoRenderer` for an
  untagged serving version. Add a `BatchPanel` (moved from the runs page) and a `LeaseView`.
- **Rationale**: the dynamic per-task architecture the spec requires (FR-231) **already exists** —
  the work is relocation + relabel + two additions, not a rebuild. Preserves the "add a modality =
  register a task-tagged model + drop in a renderer" property.
- **Alternatives considered**: a fresh per-engine page set (rejected — discards the working dynamic
  dispatch and the `serving/tasks` contract).

## R4. Allow-list discipline — the one non-UI change

- **Decision**: Extend `ui/lib/gw-allowlist.ts` with ~12 entries for already-existing endpoints:
  dataset-version detail, run detail, drift history, quality check + history, label submission, and
  the six per-engine health probes; re-section the already-present policy/suggestion entries under a
  `retraining` comment block. No proxy route logic changes. See
  [contracts/allowlist-delta.md](./contracts/allowlist-delta.md).
- **Rationale**: the BFF allow-list is the security seam (FR-032 lineage) — the operator key is
  injected only for allow-listed method+path pairs, so a foreign page cannot ride it to an arbitrary
  route. Adding a view therefore *requires* an explicit allow-list entry; this keeps that discipline
  intact and makes the delta auditable.
- **Alternatives considered**: broadening the allow-list to wildcards (rejected — defeats the seam);
  adding a backend aggregate endpoint (rejected — out of scope, backend frozen).

## R5. High-trust friction — one shared confirm primitive

- **Decision**: A single `ConfirmDialog` component backs all three high-trust actions: promote
  override (captures a typed reason), preemptive swap (names the holder to evict), and enabling
  auto-promote (warning copy). Each caller supplies its own copy + optional required-reason field.
- **Rationale**: consistency and a single audited friction path (FR-250); the override-reason and the
  preempt confirmation share the same interaction shape the operator already approved.
- **Alternatives considered**: bespoke dialogs per action (rejected — drift in copy/behaviour, more
  surface to get wrong).

## R6. Policy editor — form + JSON over one document

- **Decision**: `PolicyEditor` presents a structured form and an equivalent raw-document view over
  the **same** policy object, with a toggle; both save through the same whole-document validated PUT
  and render the structured 400 field errors inline. Auto-promote is a form control defaulting off,
  gated by `ConfirmDialog` on enable.
- **Rationale**: the form is safe/self-documenting for the common case; the JSON view matches the
  whole-document PUT exactly for expert edits and keeps a single serialization path against the
  validation contract (FR-243/244).
- **Alternatives considered**: form-only (rejected — can't express every field the contract allows
  without lag behind the backend); JSON-only (rejected — expert-only footgun for the common case).

## R7. Cross-stage hand-offs — URL params, no shared store

- **Decision**: Loop seams are deep-links carrying context in the URL query: data→training
  (`dataset@version`), training→models (registered version), serving→monitoring
  (`prediction_id` to label), retraining→models (blocked candidate to override). No global client
  store is introduced; the target stage reads its params and pre-fills.
- **Rationale**: keeps stages independently testable and stateless, matches Next.js app-router
  idioms, and makes each hand-off a shareable/bookmarkable link.
- **Alternatives considered**: a cross-stage context/store (rejected — adds shared mutable state for
  what is a one-shot param pass).

## R8. Testing posture — lint/build + browser quickstart

- **Decision**: The automated gate stays `next lint` + `next build` (type-check); behavioural
  coverage is the browser-driven per-US drills in [quickstart.md](./quickstart.md). The Python
  backend suite must stay green and unchanged (SC-141). No UI unit/e2e harness is introduced.
- **Rationale**: the `ui/` package has no test harness today; standing one up is its own feature and
  would balloon 021's scope. Type-check + lint catch the mechanical regressions a rename/relocate
  risks; the quickstart proves each user story end-to-end against a live stack.
- **Alternatives considered**: introduce Playwright/Vitest now (rejected for scope — recorded as a
  future feature); no validation guide (rejected — the quickstart is the acceptance evidence).
