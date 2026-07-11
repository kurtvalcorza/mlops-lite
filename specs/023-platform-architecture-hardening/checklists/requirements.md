# Specification Quality Checklist: Platform Architecture Hardening & Delivery Integrity

**Purpose**: Validate that the 023 brownfield specification is complete, testable, constitutionally
aligned, and ready for implementation planning/tasks.
**Created**: 2026-07-11
**Feature**: [spec.md](../spec.md)

## Content quality

- [x] CHK001 The specification describes required outcomes and user/operator value before technical
  implementation details.
- [x] CHK002 Every user story has a priority, rationale, independent test, and concrete acceptance
  scenarios.
- [x] CHK003 The architecture review distinguishes current state from the historical pre-018 review.
- [x] CHK004 Requirements use mandatory language for contracts and SHOULD only for non-binding
  internal extraction guidance.
- [x] CHK005 No unresolved template placeholders or clarification markers remain.

## Requirement completeness

- [x] CHK006 Routing integrity covers LLM, vision, standalone loading, explicit overrides, and a
  regression guard for retired ports.
- [x] CHK007 Agent security covers credential source, startup failure, public routes, all protected
  route classes, gateway injection, open-development mode, migration, constant-time comparison, and
  secret redaction.
- [x] CHK008 Delivery integrity covers clean dependency setup, backend/UI/Compose/spec jobs,
  live/hardware separation, prohibited heavyweight setup, and stable required check names.
- [x] CHK009 Migration requirements cover single source, ledger, checksum, locking, idempotency,
  legacy adoption, compatibility refusal, expand/contract, and backup/restore.
- [x] CHK010 Activation requirements cover authorities, durable state, serialization, validation,
  idempotent steps, honest resident identity, rollback, restart reconciliation, audit, and 022's
  operator-only automation boundary.
- [x] CHK011 Transport requirements cover one selected runtime, concurrency/queue/body/time bounds,
  chunked input, SSE semantics, saturation before admission, and graceful shutdown.
- [x] CHK012 Operability requirements cover bounded-cardinality metrics, local rules, remediation,
  no Alertmanager, characterization-first modularity, current docs, and historical preservation.
- [x] CHK013 Edge cases cover secret rotation/migration, cross-shell dependencies, migration crash
  windows, activation partial failure, rapid switches, oversized/chunked bodies, streaming
  disconnects, and cardinality hazards.
- [x] CHK014 Dependencies and non-goals explicitly prevent topology expansion, new resident
  services, distributed mechanisms, automatic LLM switching, GPU CI, and history rewriting.

## Testability and traceability

- [x] CHK015 Success criteria SC-152..164 are measurable and map to one or more acceptance tests or
  target-hardware drills.
- [x] CHK016 Functional requirements FR-277..328 have unique IDs continuing after 022.
- [x] CHK017 Tasks start at T490, remain unchecked for this specification-only PR, name concrete
  paths, and map implementation work to user stories.
- [x] CHK018 Tests precede correctness/security/migration/activation/transport implementation tasks.
- [x] CHK019 `[HW]` tasks cannot be satisfied by hosted CI and name required evidence.
- [x] CHK020 Quickstart covers clean setup, every user story, failure injection, backup/restore,
  complete offline checks, and target-hardware validation.

## Architecture and constitution

- [x] CHK021 The plan passes all seven constitution principles without an amendment or hidden
  violation.
- [x] CHK022 The single-GPU invariant and non-preemptable jobs remain explicit in spec, plan,
  activation contract, tasks, and quickstart.
- [x] CHK023 No Kubernetes, broker, Redis, migration daemon, notification service, or other resident
  component is introduced.
- [x] CHK024 The data model gives Postgres, MLflow, Garage, and the host agent distinct authorities
  and never infers actual residency from desired state.
- [x] CHK025 The DVC/content-addressed-registry constitution wording conflict is documented as an
  erratum and assigned a separate wording-only governance task rather than silently edited here.

## Cross-artifact consistency

- [x] CHK026 Spec, plan, research, data model, four contracts, quickstart, and tasks agree that stdlib
  is retained and ASGI removed only after parity.
- [x] CHK027 Agent public/protected route policy is consistent across spec, research, data model,
  security contract, quickstart, and tasks.
- [x] CHK028 Migration ownership and legacy-adoption behavior are consistent across all artifacts.
- [x] CHK029 022 owns LLM selection/UI resolution while 023 owns recoverable activation semantics;
  prerequisites are explicit and non-duplicative.
- [x] CHK030 Documentation distinguishes this specification's proposed target state from already
  implemented current architecture.

## Notes

- Checklist validates specification quality only; it does not mark any implementation task done.
- Any change to an authority, public route, migration ownership, activation state, or GPU invariant
  requires re-running this checklist and the automated spec consistency check.
