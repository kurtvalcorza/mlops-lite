# Implementation Plan: MLOps-Lite Audit Hardening

**Branch**: `005-hardening` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/005-hardening/spec.md` (closes the 2026-06-28 Codex audit)

## Summary

Close the audit's legitimate findings without changing lifecycle or UI behavior: (US1) bind every
Compose-published port to loopback by default; (US2) make the gateway fail-closed when no key is set,
with an explicit dev override; (US3) give the operator CLIs API-key support; (US4) make the BFF's
origin set configurable and its errors non-leaky; (US5) document key-rotation + test-runner behavior.
Phase-gated like 002/004, validated live each phase, never regressing 001–004.

## Technical Context

**Language/Version**: YAML (Compose), Python 3.11 (gateway auth + CLIs + tests), TypeScript (BFF). No
new languages, no new dependencies.

**Primary Dependencies**: none added. Loopback binding is a Compose `ports` prefix; fail-closed is a
small `auth.py` change; CLI keys reuse the existing `tests/_auth.py` header pattern; BFF changes are
plain route-handler/env code.

**Storage**: none. Secrets still come from the local `.env` (002).

**Target Platform**: Win11 + WSL2 + Rancher Desktop. The one platform-specific risk: confirming
loopback-published MinIO/MLflow stay reachable from the Ubuntu WSL daemons over `localhost`.

**Project Type**: security + operational hardening over 002/004 — touches `docker-compose.yml`,
`gateway/app/auth.py` (+ `main.py`), `data/register_dataset.py`, `monitoring/drift.py`,
`ui/app/api/gw/[...path]/route.ts`, `.env.example`, README, `pyproject.toml`, tests.

**Performance Goals**: none affected (binding/auth/env checks are negligible).

**Constraints**: localhost-only posture preserved and strengthened; no lifecycle change; the keyed
provisioned path (`gen_secrets`/`up_all`) must behave exactly as today; the six tabs unchanged.

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Loopback binding **removes** LAN exposure — strictly more local-first | ✅ strengthened |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | 005 touches networking/auth/CLIs only; never the VRAM mutex | ✅ unchanged |
| III. Lightweight Footprint | No new service/image/dependency | ✅ |
| IV. Full Lifecycle Coverage | No stage added/dropped | ✅ N/A |
| V. OSS & Swappable | No vendor change | ✅ |
| VI. Reproducibility & Observability | Auth posture clearer; rotation documented; observability unchanged | ✅ |
| VII. Phase-Gated Delivery | Five independently-runnable stories (US1–US5), each verifiable on the target machine | ✅ |
| Workflow: "no new runtime without amendment" | None introduced | ✅ no amendment |

**No amendment required.** 005 hardens within the existing constitution; US1 in particular advances
Principle I. Clean gate-check, mirroring 002/004.

## Project Structure

### Source Code (delta over 004)

```text
mlops-lite/
├── docker-compose.yml            # MODIFIED: ${BIND_ADDR:-127.0.0.1}: prefix on all 7 port publishes
├── .env.example                  # MODIFIED: document BIND_ADDR + GATEWAY_ALLOW_OPEN
├── gateway/app/
│   ├── auth.py                   # MODIFIED: fail-closed default; GATEWAY_ALLOW_OPEN dev override; rotation doc
│   └── main.py                   # MODIFIED (if needed): surface auth-mode on startup / refuse-start path
├── data/register_dataset.py      # MODIFIED: --api-key / env / file -> X-API-Key
├── monitoring/drift.py           # MODIFIED: same key support
├── ui/app/api/gw/[...path]/route.ts  # MODIFIED: UI_ALLOWED_ORIGINS (+ [::1]); generic unreachable error
├── pyproject.toml / README.md    # MODIFIED: test-runner clarity (FR-047) + rotation note (FR-046)
└── tests/
    ├── test_exposure.py          # NEW: loopback bound, LAN refused, daemons still reach MinIO/MLflow
    ├── test_auth.py              # EXTENDED: fail-closed default; override opens; keyed unchanged
    ├── test_cli_auth.py          # NEW: dataset/drift CLIs succeed with a key against auth-on gateway
    └── test_ui_security.py       # EXTENDED: configured origin accepted; error carries no upstream URL
```

**Structure Decision**: Each finding maps to one small, contained change in its own file; the only
cross-cutting risk (loopback vs WSL daemon reachability) gets its own test (`test_exposure.py`).
Nothing in the gateway lifecycle routers or the native daemons changes.

## Phasing (maps to constitution VII)

- **Phase 1 — Loopback binding (US1)**: `${BIND_ADDR:-127.0.0.1}:` on all 7 ports; verify LAN refused
  + WSL daemons + browser still reach everything. Exit: SC-025.
- **Phase 2 — Fail-closed auth (US2)**: `auth.py` default-closed + `GATEWAY_ALLOW_OPEN` override
  (loud warning); provisioned path unchanged. Exit: SC-026.
- **Phase 3 — CLI auth parity (US3)**: key support in the dataset + drift CLIs. Exit: SC-027.
- **Phase 4 — BFF robustness (US4)**: configurable origins (+`[::1]`) + generic errors. Exit: SC-028.
- **Phase 5 — Docs & test hygiene (US5)**: rotation note + pytest clarity. Exit: SC-029.

Cross-cutting: a no-regression pass of the full 001–004 suite with loopback binds + auth on.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| `BIND_ADDR` override (not hardcode `127.0.0.1`) | An operator may genuinely need LAN exposure; an env keeps the secure default while allowing an explicit opt-in | Hardcoding loopback blocks legitimate exposure and invites a fork; defaulting to loopback with an override is the secure-by-default middle |
| Fail-closed + `GATEWAY_ALLOW_OPEN` (not always-closed) | Preserves a frictionless local dev escape hatch while flipping the *default* to closed | Always-closed breaks the documented 002 open-dev convenience with no override; always-open is the audited risk |
| Reuse `tests/_auth.py` header pattern in CLIs | One consistent key-attach contract across tests + CLIs | A bespoke per-CLI scheme drifts from the tested behavior |
| `test_exposure.py` gates US1 | Loopback-vs-WSL reachability is the one real regression risk; it must be proven, not assumed | Shipping the bind change without a reachability test risks silently breaking daemon→MinIO/MLflow |
