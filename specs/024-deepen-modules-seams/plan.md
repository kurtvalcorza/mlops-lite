# Implementation Plan: Codebase Architecture Hardening — Deepen Modules & Testability Seams

**Branch**: `024-deepen-modules-seams` (working branch `claude/codebase-architecture-improvements-udog5o`) | **Date**: 2026-07-22 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/024-deepen-modules-seams/spec.md`

## Summary

Finish three extractions the codebase already believes in, without changing behavior: (1) decompose
`platformlib/store.py` into per-aggregate repositories under `storeimpl/` plus a separated object-store
module, behind the existing test-pinned re-export facade; (2) lift the LLM go-live *ordering* out of the
FastAPI+httpx router into a web-free `gateway/app/promotion.py` so it unit-tests like the other domain
cores, with the router reduced to an outcome→HTTP/metric adapter; (3) turn the agent's hand-rolled
GET/POST if-ladders into a stdlib-only route table. Record accepted and rejected decisions as ADRs. The
overriding technical approach is **behavior preservation proven by test parity** — the existing offline
suite passes unchanged, every extracted seam gains a web-free unit test, and the live ordering test
passes on a brought-up stack.

## Technical Context

**Language/Version**: Python 3.11 (gateway, host agent, platformlib); no new language.

**Primary Dependencies**: unchanged. Gateway keeps FastAPI/httpx/pydantic; host agent stays **stdlib-only**;
`platformlib` stays importable with boto3/psycopg lazily imported. **No new dependency is added** (FR-014).

**Storage**: Postgres `gateway` DB (relational store) + Garage S3 (object store), both already present.
No schema change is planned; if one becomes genuinely necessary it lands as a NEW numbered
`platformlib/migrations/*.sql` (FR-016).

**Testing**: pytest offline suite (no fastapi/httpx installed — the dependency line this feature respects),
`test_*.py` house pattern with in-memory fakes (`tests/_activation.py` style); live legs gated by
`conftest` guard fixtures.

**Target Platform**: Linux/WSL single machine (constitution Principle I). No GPU touched by this feature.

**Project Type**: internal refactor of an existing web-service + native-agent + shared-library monorepo.

**Performance Goals**: no runtime performance change intended; the store decomposition must not add import
cost (drivers stay lazy) or extra queries (the predictions⋈labels window join is preserved).

**Constraints**: dependency-light (Principle III); stdlib-only agent; fail-open on prediction/label/capture
WRITES and fail-loud on window/policy/job READS preserved (FR-015); public agent surface byte-preserved
(FR-012); exactly one gated promotion choke-point and one live-switch caller (FR-008/SC-006).

**Scale/Scope**: ~28 `from platformlib import store` call sites (must stay unchanged); `store.py` ~630 LOC →
facade; 6 relational aggregates to home; 1 router handler (~48 LOC) to thin; ~220 LOC of agent dispatch to
table-ize; ~4–6 ADRs.

## Constitution Check

*GATE: passes before Phase 0 and re-checked after Phase 1.*

| Principle | Verdict | Notes |
|---|---|---|
| I. Local-First, Single-Machine | ✅ Pass | No new service, no cloud, no network surface. |
| II. Single-GPU, On-Demand (NON-NEGOTIABLE) | ✅ Pass / reinforced | No admission or serving change. FR-008 keeps the LLM live-switch to a single caller — it *hardens* the one-tenant/one-go-live-path invariant (FR-275/307/313). |
| III. Lightweight Footprint | ✅ Pass / reinforced | FR-014 forbids new heavy deps; drivers stay lazy so idle import cost is unchanged. |
| IV. Full Lifecycle Coverage | ✅ Pass | No lifecycle stage added or dropped. |
| V. Open-Source & Swappable | ✅ Pass / reinforced | Extractions sharpen the interfaces (repository per aggregate; go-live use-case behind an outcome enum) — more swappable, not less. |
| VI. Reproducibility & Observability | ✅ Pass | `REGISTRY_OPS` labels and health/metrics surfaces preserved byte-for-byte (FR-007/FR-012). |
| VII. Incremental, Phase-Gated | ✅ Pass | Three independently-testable/deployable PRs, sequenced P1→P2→P3; ADRs ship with their code. |

**Result**: no violations; Complexity Tracking not required. Any external-contract or schema change that
surfaces during implementation is an explicit, called-out deviation gated by FR-016 and must be re-checked
here before it lands.

## Project Structure

### Documentation (this feature)

```text
specs/024-deepen-modules-seams/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions + rejected alternatives
├── data-model.md        # Phase 1 — module/seam model, go-live outcome enum, repository interfaces
├── quickstart.md        # Phase 1 — how to validate (offline suite, web-free seam tests, live leg)
├── contracts/
│   └── preservation.md  # Phase 1 — surfaces that MUST NOT regress (facade, promote I/O, agent routes)
├── checklists/
│   └── requirements.md  # spec quality checklist (from /speckit-specify)
└── tasks.md             # Phase 2 — created by /speckit-tasks, NOT here
```

### Source Code (repository root)

```text
platformlib/
├── store.py                     # US1: reduced to a thin re-export facade (no aggregate SQL inline)
├── objectstore.py               # US1: NEW — S3 client + paginated listings, split off store.py
└── storeimpl/
    ├── _base.py                 # existing: StoreError/LabelExists + epoch/json seam helpers
    ├── activations.py           # existing: already-extracted activation repository (the proven seam)
    ├── predictions.py           # US1: NEW — predictions aggregate repository
    ├── labels.py                # US1: NEW — write-once labels repository
    ├── capture.py               # US1: NEW — capture-index repository
    ├── jobs.py                  # US1: NEW — jobs repository
    ├── policies.py              # US1: NEW — policies repository
    └── suggestions.py           # US1: NEW — promotion-suggestions repository

gateway/app/
├── promotion.py                 # US2: NEW — web-free go-live ordering + GoLiveOutcome (no fastapi/httpx)
└── routers/models.py            # US2: promote() reduced to translate→call→map(outcome→HTTP/REGISTRY_OPS)

hostagent/
└── main.py                      # US3: handle_get/handle_post → ordered stdlib route table; handlers callable

docs/
└── adr/                         # US4: NEW — one ADR per accepted decision + rejected alternative

tests/
├── test_store_facade.py         # existing: pins the re-export surface (US1 guard)
├── test_store_decomposition.py  # US1: NEW — each aggregate repository, web-free
├── test_promotion_ordering.py   # US2: NEW — go-live outcomes + refuse-before-alias, web-free
├── test_promote_ordering.py     # existing (this branch): live ordering leg (SC-003)
└── test_agent_routes.py         # US3: NEW — handlers callable without the HTTP server
```

**Structure Decision**: no new top-level packages; every change extends an existing home
(`platformlib/storeimpl/`, `gateway/app/`, `hostagent/`, `docs/`). This keeps the diff a set of
*moves + a thin adapter* rather than a re-layout, which is what makes test-parity a credible gate.

## Design Phases

### Phase 0 — Research
See [research.md](./research.md): the extraction strategy for each candidate, why the go-live paths are
**not** merged (the grill's rejected alternative), ADR placement, and the test-parity gate. All spec
decisions were resolved during `/speckit-specify` clarifications — no open NEEDS CLARIFICATION.

### Phase 1 — Contracts and models
- [data-model.md](./data-model.md): the seam model — repository interface shape, the `GoLiveOutcome` enum
  and result object, and the agent route-table entry shape.
- [contracts/preservation.md](./contracts/preservation.md): the surfaces that MUST NOT regress — the store
  facade symbol set, the promote request/response + status/metric mapping, and the agent public route set.
- [quickstart.md](./quickstart.md): the validation recipe (offline suite green without fastapi/httpx →
  web-free seam tests → live ordering leg on `make up`).

### Phase 2 — Tasks
Created by `/speckit-tasks` (not here): a dependency-ordered list, one slice per aggregate/candidate, each
gated by its web-free test and the unchanged facade/route contracts.

## Complexity Tracking

No constitution violations — section intentionally empty.
