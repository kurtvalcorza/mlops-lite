# Implementation Plan: MLOps-Lite Hardening

**Branch**: `002-hardening` | **Date**: 2026-06-27 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/002-hardening/spec.md`

## Summary

Operational hardening over the feature-complete platform (001): add **API-key auth** + **secret
hygiene** to the gateway/stack; **supervise** the native WSL daemons (start, health, restart) and
fold them into a **single-command** bring-up/teardown; and provide an **idempotent bootstrap** that
proves the genericized template is portable to a clean machine. No lifecycle behavior changes —
US1–US5 keep working unchanged behind a valid key. Delivered in four phase-gated slices.

## Technical Context

**Language/Version**: Python 3.11 (gateway middleware), Python 3.12 stdlib (native supervisor),
Bash/PowerShell (bootstrap + bring-up), YAML for Compose.

**Primary Dependencies**: No new heavy runtime. Gateway gains a small auth dependency (FastAPI
dependency/middleware + `hmac`/`secrets` from stdlib for constant-time key checks). The daemon
**process supervisor** is pure stdlib (mirrors the existing `supervisor.py`/`trainer.py` pattern).
Secrets via a git-ignored `.env`/secret file; no IdP, no Vault.

**Storage**: unchanged (Postgres, MinIO). Credentials move from compose defaults to a local secret
file; the API-key set lives in the same local secret source (hashed in memory).

**Target Platform**: unchanged — Docker Compose + native WSL daemons on one Win11 + WSL2 machine
(hardware-profile.md). Portability target is a second machine of the same shape.

**Project Type**: operational/cross-cutting hardening of the existing multi-service repo.

**Performance Goals**: auth adds negligible per-request overhead (constant-time hash compare);
supervisor restart of a crashed daemon within a bounded time (target ≤ ~15 s incl. health wait).

**Constraints**: stay Local-First (no cloud auth), Lightweight (no new always-on service beyond a
tiny supervisor process), and preserve Principle II (the supervisor must not change the
one-model-in-VRAM mutex between serving and training).

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Auth is a local API key; secrets in a local file; no cloud/IdP | ✅ no cloud dependency added |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | Supervisor only *starts/restarts* daemons; the serving↔training VRAM mutex is unchanged | ✅ no change to Principle II logic |
| III. Lightweight Footprint | Supervisor is one tiny stdlib process; auth adds no service; no new image weight | ✅ within budget |
| IV. Full Lifecycle Coverage | No stage added or dropped — purely operational | ✅ N/A (no lifecycle change) |
| V. OSS & Swappable | Stdlib auth + supervisor behind clear seams; swappable for a real IdP later | ✅ |
| VI. Reproducibility & Observability | Daemon states surfaced via gateway/metrics; bootstrap is idempotent + recorded | ✅ extends FR-015 |
| VII. Phase-Gated Delivery | 4 independently-runnable phases, each verifiable on the target machine | ✅ (see Tasks) |
| Gate Zero: GPU access | Bootstrap verifies `nvidia-smi` / native GPU before serving/training | ✅ FR-020 |

**No amendment required** — 002 hardens operation within the existing principles; it does not need
more than one live model, add a cluster/orchestrator, or drop a stage. Auth stays single-operator
(an assumption-lift, not a multi-tenant feature).

## Project Structure

### Documentation (this feature)

```text
specs/002-hardening/
├── plan.md              # This file
├── spec.md              # Feature specification
└── tasks.md             # Task list (/speckit-tasks)
```

### Source Code (delta over 001)

```text
mlops-lite/
├── gateway/app/
│   ├── auth.py                 # NEW: API-key dependency (constant-time), open-list for health/metrics
│   └── main.py                 # MODIFIED: apply auth dependency to protected routers
├── supervisor/                 # NEW: native process supervisor (pure stdlib)
│   └── supervise.py            #   start/health/restart the serving, training, vision daemons
├── scripts/
│   ├── up_all.ps1 / up_all.sh  # NEW: one-command bring-up (infra + daemons + IP wiring)
│   ├── down_all.ps1            # NEW: one-command teardown (no GPU orphans)
│   ├── bootstrap.sh            # NEW: idempotent native-env provisioning + gate zero
│   └── gen_secrets.* / .env.example  # MODIFIED: generate/require secrets, drop hardcoded defaults
├── docker-compose.yml          # MODIFIED: credentials from env/secret file, no inline defaults
└── tests/
    ├── test_auth.py            # NEW: 401 without key, 200 with key, health/metrics open
    ├── test_supervisor.py      # NEW: kill a daemon → restarted healthy within bound
    └── test_portability.py     # NEW (manual-tier): clean bootstrap → serving smoke passes
```

**Structure Decision**: Hardening is layered onto 001 in place — an auth seam in the gateway, a new
`supervisor/` peer to the existing native daemons, and bring-up/bootstrap scripts. Nothing in the
lifecycle code paths (infer/models/datasets/runs/monitor/vision) changes except that protected
routes now require a key.

## Phasing (maps to constitution VII)

- **Phase 1 — Auth & secret hygiene (US1)**: gateway API-key dependency; move credentials to a
  local secret file; fail-fast on missing secrets. Exit: protected endpoints 401 without a key,
  200 with one; no committed production secret; 001 tests pass with a key.
- **Phase 2 — Daemon supervision (US2)**: stdlib supervisor that starts/health-checks/restarts the
  three native daemons with backoff. Exit: kill-a-daemon → auto-restart healthy; gateway reflects
  state.
- **Phase 3 — One-command bring-up/teardown (US3)**: `up_all`/`down_all` wrapping Compose +
  supervisor + IP injection. Exit: one command to ready, one to stopped with no GPU orphans.
- **Phase 4 — Bootstrap & portability (US4)**: idempotent `bootstrap.sh` (venv/deps/seed/gate-zero)
  + portability check. Exit: clean environment → edit hardware-profile.md → serving smoke passes;
  re-run is a no-op.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Custom stdlib supervisor (not systemd/supervisord) | Daemons run in WSL behind the gateway; a tiny stdlib supervisor matches the existing daemon pattern and stays dependency-free (Principle III) | systemd in WSL is inconsistent across setups; supervisord adds a dependency for 3 processes |
| API-key auth (not OAuth/IdP) | Single local operator just needs to close open access (Principle I) | An IdP/SSO violates Local-First and Lightweight for a one-operator localhost platform |
| Secret file over a secrets manager | Local-first, one machine; a file ignored by git is sufficient and auditable | Vault/cloud KMS adds an always-on dependency and a cloud tie against Principle I/III |
