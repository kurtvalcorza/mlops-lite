# Specification Quality Checklist: 028 Model-Selective Serving

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-09
**Feature**: [spec.md](../spec.md)

## Content Quality

- [~] No implementation details (languages, frameworks, APIs)
- [X] Focused on user value and business needs
- [~] Written for non-technical stakeholders
- [X] All mandatory sections completed

## Requirement Completeness

- [X] No [NEEDS CLARIFICATION] markers remain
- [X] Requirements are testable and unambiguous
- [X] Success criteria are measurable
- [~] Success criteria are technology-agnostic (no implementation details)
- [X] All acceptance scenarios are defined
- [X] Edge cases are identified
- [X] Scope is clearly bounded
- [X] Dependencies and assumptions identified

## Feature Readiness

- [X] All functional requirements have clear acceptance criteria
- [X] User scenarios cover primary flows
- [X] Feature meets measurable outcomes defined in Success Criteria
- [~] No implementation details leak into specification

## Notes

**Both clarifications were decided by the owner on 2026-08-09 and are now written as requirements.**

- **FR-456 (thrash bound) → minimum residency window.** A newly resident model is not an eligible
  eviction victim for a configured period; a placement that would need to evict inside that window is
  refused `gpu_busy` with a `Retry-After` computed from the time remaining. Chosen over a per-tenant
  rate limit (bounds blame, not GPU churn) and over protecting recently-served victims (deadlocks
  when every resident is active). Expanded into FR-456a–FR-456c so the bound, the refusal, and the
  job-path exemption are each testable, and SC-209 restated as a per-period eviction count.
- **FR-457 (version pinning) → promoted versions only.** A qualifier is an assertion, not a
  selection: `name:version` is served only when that version is the promoted one, so a tenant request
  can never place a version 022's gated promotion path did not promote. FR-457a requires the refusal
  to name the version that *is* promoted, which is what separates "promotion moved under me" from "I
  pinned something never promoted".

Both were surfaced as decisions rather than guessed. FR-456 trades tenant fairness against GPU
utilization; FR-457 decides whether tenant requests can route around promotion governance. Neither
has a default that could be defended as obvious.

**The three `[~]` items are a deliberate, precedented deviation, not an oversight.** This spec
names concrete components (`hostagent/coordinator.py`, `model_key`, `SERVING_URL`,
`BROKER_COORDINATOR_ADMISSION`) and cites file paths. The template asks specs to avoid that.
The reason it is warranted here: 028 exists **because three implemented seams disagree with an
already-written contract**, and a description of the defect that cannot name the seams cannot be
checked against the code. Specs 026 and 027 in this repo set the same precedent, and the audience
for this platform's specs is its operators and maintainers, not an external business stakeholder.

The deviation is bounded: every **requirement** (FR-439–FR-466) is stated as an observable
behavior, and every **success criterion** (SC-204–SC-210) is checkable from a client's or an
operator's position without reading source. SC-207 references the admission state endpoint
because 026 established that endpoint as the observation surface for exactly this invariant.

**`/speckit-analyze` remediation, 2026-08-09.** All ten findings applied. The load-bearing ones:
**F1** — the feature description named a `POST /v1/completions` endpoint that does not exist, and
omitted the ASR and vision surfaces that do and that carry the same defect; the Input quote is
corrected in place and T782/T782a fix the wiring. **D1** — the plan asserted a Principle III host-RAM
obligation that no requirement carried and no task enforced; now FR-467 (admission precondition),
FR-468 (calibrate against a measurement), SC-211, T793/T815a/T817. **A1** — the conditional scope
boundary for ASR/vision ("unless the model-keyed shim makes it free") is now a decision.
Traceability: FR coverage 24/32 → **34/34**, SC 4/7 → **8/8**, unmapped tasks 21/54 → **8/57**, all
eight genuinely infrastructural.

**Downstream artifacts**: `plan.md`, `research.md`, `data-model.md`, `quickstart.md`, `contracts/`,
`tasks.md` all written.

Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`.
