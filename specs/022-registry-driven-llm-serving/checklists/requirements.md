# Specification Quality Checklist: Registry-Driven LLM Serving & Operator Model Selection

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-07-05
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- **Behaviour-level FR/SC, house convention (018–021)**: functional requirements and success
  criteria stay at the behaviour level; the concrete mechanism (the registry `@serving` resolver,
  the host-agent controlled swap, the base+adapter artifact loading) is confined to the Overview,
  Assumptions, and Dependencies sections as the decision record. Domain vocabulary that the whole
  platform is built on — GPU lease / one-tenant VRAM (Principle II), LoRA adapter, model registry —
  is used deliberately and is not an implementation leak.
- **ID space**: continues the shared FR/SC space — FR-254..276, SC-143..151 (prior max FR-253,
  SC-142; FR-275 added by the 2026-07-05 clarification — operator-only automation boundary; FR-276
  added by the PR #64 spec review — serving-target authoritative to the active pointer). Tasks
  continue from T461.
- **Spec review (PR #64, @claude; @codex rate-limited)**: 3 code-grounded findings fixed in-spec —
  §1 the same-tenant LLM model switch needs an explicit force-reload (unload → ensure_loaded), since
  `preempt_for` no-ops when the holder tenant already == the target engine (research R4, contract,
  T466); §2 the adapter's `/infer` response `model` echo must use the resolved identity, not the
  static `MODEL_ALIAS` env (FR-262, T465); §3 `serving/tasks` must be filtered to the active pointer
  so a previously-promoted LLM isn't a stale duplicate (FR-276, T479). Plus the R2 base-resolution
  rule made explicit (direct full-model base; adapter-of-adapter = resolution error). No Principle II
  violation was found; the one-tenant invariant holds throughout.
- **Clarifications (2026-07-05)**: 4 decisions ratified — promote = immediate go-live activation
  (FR-255), operator-only served-LLM switching (FR-275), scope kept to the LLM serving quirks (Out of
  Scope), immediate controlled reload (FR-255). All four matched the recommended options; no spec
  contradiction introduced; tasks.md already honors them (no auto-switch task; T465–T467 are
  operator-initiated + immediate-reload).
- **Priorities**: US1 (promote-to-serve) and US2 (honest served identity) are P1 — together they are
  the correctness-complete MVP that makes the LLM a registry-driven, console-operable engine. US3
  (serve LoRA fine-tunes) and US4 (fine-tunes discoverable) are P2 — they make a *fine-tune* usable
  end-to-end. US5 (safe switch under contention) is P3 — it hardens the switch against a resident
  holder / running job.
- **Backend + frontend**: unlike 021 (front-end only), this feature necessarily changes the backend
  (LLM engine artifact resolution, registry descriptors/lineage, gateway served-identity reporting)
  in addition to the console surface. The plan must own both halves.
- All items pass on the first validation pass; no [NEEDS CLARIFICATION] markers were required — the
  four quirks and their fix were established empirically while driving the live 021 console.
