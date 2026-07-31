# Deferred Scope Handoff: MVP 2 (027) and MVP 3 (028)

**Owner**: increment 026 · **Consumed by**: 027, 028 · **Status**: recorded, not implemented

US11 and US12 are **specified** in [spec.md](./spec.md) so the information architecture and contracts
are designed whole, but they are **built** in later increments (plan.md Design Phases, Principle VII).

This file exists so the handoff has a home **inside 026**. It deliberately does *not* create a
partial `specs/027-*/` or `specs/028-*/` directory: `scripts/check_specs.py` requires the full
six-artifact set (`spec.md`, `plan.md`, `tasks.md`, `research.md`, `data-model.md`, `quickstart.md`)
for every feature directory, so a stub containing only a spec would fail the `specs` job — a
**required** CI gate since 023 US3. A deferral note must not break the build that guards the specs.

---

## MVP 2 → 027: lifecycle write actions (US11)

**Scope**: start and cancel jobs, register versions, assign aliases, create endpoint assignments,
submit labels, approve review items, manage capture and evaluation policies.

**What 026 guarantees on its behalf** (verified by T695):

1. **Every route 026 adds is read-only.** No `gateway/app/console/*` or `runtime/*` route mutates
   state. The single write-shaped entry, `POST /console/predictions/{id}/payload`, only reveals a
   payload that already exists — it is a `POST` solely so the identifier travels in the body rather
   than a URL (SC-192), not because it changes anything.
2. **The write surface has a designed home.** MVP 2 lands its own contract; it does not extend
   `contracts/console-read-api.md`, whose cross-cutting rule 4 states that no route in it mutates.

**Constraints 027 inherits and must not relax**:

- **FR-435** — alias assignment routes through the **gated promotion endpoint**. Writing the registry
  directly would bypass the 011 evaluation gate, which is the platform's central safety mechanism.
  The console must never become the way around the gate.
- **FR-436** — optimistic updates roll back on upstream failure and surface the upstream error
  verbatim. A console that silently keeps an optimistic value after a failed write is worse than one
  with no optimism at all.
- **Principle II** — no write action may preempt a running job, and none may alter admission
  semantics. FR-379's ban on job-preempting controls carries forward unchanged.
- The read model and conflict semantics (US10) must be **proven** before writes layer on. Mutating
  state across five systems of record without a trustworthy read surface is how a console corrupts a
  platform.

---

## MVP 3 → 028: operational intelligence (US12)

**Scope**: drift workflows, suggestion review with evidence, quality-gate authoring, automated
cross-system reconciliation, controlled rollout and rollback, audit views.

**What 026 guarantees on its behalf** (verified by T696):

1. **Affordances surfaced early are inert and labelled.** The conflict banner (T659) offers a
   `reconcile` action because `StateConflict.suggestedAction` includes it in the data model — in 026
   it performs **no reconciliation**. An affordance that looks actionable but does nothing teaches
   operators that the console lies, which is precisely the trust this increment is trying to build.
2. **Nothing is auto-applied.** No suggestion is auto-accepted or auto-applied anywhere in the
   console (FR-437). Suggestions carry evidence and remain recommendations.

**Constraints 028 inherits**:

- **FR-437** — acceptance is recorded as an **operator decision**, never automatic.
- **Reconciliation is a write path** and therefore also bound by the MVP 2 constraints above.
- Rollout and rollback controls remain bound by **FR-418**: only controls the gateway actually
  implements may be rendered, resolved through `GET /console/capabilities`. Shipping the *UI* for a
  rollout the backend cannot perform is the decorative-control failure 026 explicitly forbids.

---

## Open items 026 hands forward

| Item | Origin | Note |
|---|---|---|
| Automated reconciliation | research R9 | 026 computes conflicts per observation and never persists them. Reconciliation needs durable conflict state, and therefore a migration — the first real schema change since 023 US4. |
| Endpoint persistence | research R7 | 026 synthesizes endpoints deliberately, to avoid a second thing that can disagree with the serving pointer. If MVP 2 needs per-endpoint config, revisit — with that duplication risk stated explicitly. |
| Multi-host orchestration | FR-382 | 026 ships the multi-host *contract shape* only. Actual multi-host operation is out of scope for all three MVPs and would touch Principle I. |
| User identity | research R13 | Owner/approver fields are omitted, not stubbed. If MVP 2 needs real attribution, a user model is its own increment — not a rider on a write-path increment. |
