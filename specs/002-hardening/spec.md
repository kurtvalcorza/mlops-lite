# Feature Specification: MLOps-Lite Hardening

**Feature Branch**: `002-hardening`

**Created**: 2026-06-27

**Status**: Draft

**Input**: User description: "Harden the completed platform (001) for safe, repeatable real use:
require authentication on the gateway and stop shipping hardcoded secrets; supervise the native
GPU/vision daemons so they start, restart, and report health without manual `bash run.sh`; bring
the whole platform (Compose infra + native daemons) up and down from one command; and make the
genericized template actually portable — a clean machine should reach a passing smoke test by
editing only the hardware profile."

> **Scope note**: 002 *extends* 001 (the feature-complete platform) — it changes no lifecycle
> behavior (US1–US5) and adds no lifecycle stage. It lifts three v1 assumptions from 001
> ("single local operator; no auth", "manual daemon start", "design-target one machine") into
> hardened, repeatable operation. Requirement IDs continue 001's space (FR-016+, SC-008+).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Secure the gateway and stop shipping secrets (Priority: P1)

The operator turns on authentication so the gateway is not open to anyone who can reach the host,
and the platform no longer ships hardcoded credentials. Inference and all mutating endpoints
require a valid API key; health and metrics stay open for probes. Object-store, database, and
dashboard credentials come from a local secret file or generated values, not committed defaults.

**Why this priority**: The platform now trains, serves, and stores artifacts; leaving it
unauthenticated with `minioadmin/minioadmin`-class defaults is the single biggest real-use risk.
This is the highest-value hardening with the least surface area.

**Independent Test**: With auth enabled, a request without a key (or a wrong key) to `/infer`,
`/models`, `/datasets`, `/runs`, or `/monitor/check` is rejected; the same request with a valid
key succeeds. A fresh bring-up fails fast if required secrets are unset rather than using a known
default.

**Acceptance Scenarios**:

1. **Given** auth is enabled, **When** a request to a protected endpoint arrives without a valid
   API key, **Then** it is rejected with `401` and no internal detail is leaked.
2. **Given** a valid API key, **When** the same request is made, **Then** it succeeds exactly as
   before (no behavior change to US1–US5).
3. **Given** `/healthz` and `/metrics`, **When** probed without a key, **Then** they still respond
   (so liveness/Prometheus keep working).
4. **Given** a clean checkout, **When** the operator brings the stack up without configuring
   secrets, **Then** bring-up fails with clear guidance instead of falling back to a shipped
   default credential.

---

### User Story 2 - Supervise the native daemons (Priority: P2)

The operator no longer hand-starts each native WSL daemon. A lightweight supervisor starts the
serving, training, and vision daemons, monitors their health, and restarts them on failure with
backoff — surfacing each daemon's state through the gateway.

**Why this priority**: Today the GPU/vision services are started by hand and silently die on
crash; that is the main barrier to leaving the platform running. Depends on nothing but 001's
daemons.

**Independent Test**: Start the supervisor; confirm all three daemons report healthy. Kill one;
confirm the supervisor restarts it within a bounded time and the gateway reflects the transient
unhealthy → healthy transition.

**Acceptance Scenarios**:

1. **Given** the supervisor is running, **When** it starts, **Then** the serving, training, and
   vision daemons each reach a healthy state without manual per-daemon commands.
2. **Given** a daemon process is killed, **When** the supervisor detects it, **Then** it is
   restarted with backoff and returns to healthy, without restarting the others.
3. **Given** a daemon is crash-looping, **When** restarts exceed a threshold, **Then** the
   supervisor backs off and surfaces a persistent-unhealthy state rather than hammering.
4. **Given** the supervisor manages the daemons, **When** the operator queries the gateway,
   **Then** each daemon's reachability/health is reported (extends `/serving/health`,
   `/training/health`, `/vision/health`).

---

### User Story 3 - One-command bring-up and teardown (Priority: P3)

The operator brings the **entire** platform — Compose infrastructure and the native daemons —
up (and down) with a single command, with daemon IPs wired automatically.

**Why this priority**: 001's FR-013 single-command bring-up only covers Compose; the hybrid-GPU
daemons and the IP injection are still manual. This closes that gap and is the natural companion
to US2.

**Independent Test**: From a stopped state, one command reaches a fully-ready platform (infra
healthy + daemons healthy + gateway able to reach all daemons); one command returns it to
stopped.

**Acceptance Scenarios**:

1. **Given** a stopped platform, **When** the operator runs the bring-up command, **Then** infra
   and all native daemons reach ready and the gateway resolves every daemon.
2. **Given** a running platform, **When** the operator runs teardown, **Then** Compose services
   and native daemons all stop cleanly with no orphaned GPU processes.

---

### User Story 4 - Reproducible bootstrap & portability (Priority: P4)

A clean machine is provisioned to run the platform by editing only the hardware profile and
running an idempotent bootstrap: it sets up the native environment (Python venv + deps, model/
weight seeding, GPU build/availability check), verifies gate zero, and reaches a passing serving
smoke test — validating the "genericized template" claim.

**Why this priority**: Highest-effort, lowest-urgency; proves portability but isn't needed for
the operator's current machine. Depends on US1–US3 being in place to bootstrap a *hardened* setup.

**Independent Test**: On a clean environment (or a pruned clone), edit only
`hardware-profile.md`, run the bootstrap, and confirm it reaches the serving smoke test without
manual fix-ups; re-running the bootstrap is a no-op (idempotent).

**Acceptance Scenarios**:

1. **Given** a clean checkout and an edited hardware profile, **When** the bootstrap runs,
   **Then** it provisions the venv/deps, seeds required weights, verifies GPU access (gate zero),
   and the serving smoke test passes.
2. **Given** an already-provisioned machine, **When** the bootstrap is re-run, **Then** it makes
   no destructive changes and reports already-ready (idempotent).

---

### Edge Cases

- **Missing secret on bring-up**: a required key/credential is unset — fail fast with guidance;
  never silently fall back to a shipped default (FR-017).
- **Auth must not break internals**: gateway → native-daemon calls and offline operation keep
  working with auth enabled; the daemons sit behind the gateway, not the public surface.
- **Key handling**: API keys are never logged or echoed in errors; comparison is constant-time.
- **Crash loop**: a daemon that won't stay up triggers supervisor backoff and a clear unhealthy
  signal, not an infinite fast-restart.
- **GPU absent on a new machine**: gate zero fails clearly during bootstrap, before any serving
  or training work (consistent with the constitution's Gate Zero).
- **Teardown leaves no GPU orphans**: stopping the platform releases VRAM and kills daemon
  subprocesses (e.g., `llama-server`).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-016**: The gateway MUST require a valid API key on all inference and mutating endpoints;
  `/healthz` and `/metrics` MAY remain unauthenticated for liveness and Prometheus. Keys are
  configured locally (env or secret file) — no managed identity provider (Principle I/III).
- **FR-017**: Service credentials (object store, database, dashboards) MUST be supplied via local
  secrets and MUST NOT ship as hardcoded defaults in committed files; bring-up MUST fail clearly
  when a required secret is missing rather than using a known default.
- **FR-018**: The native GPU/vision daemons (serving, training, vision) MUST be started,
  health-monitored, and restarted-on-failure with backoff by a lightweight supervisor — no manual
  per-daemon invocation, no crash-loop hammering.
- **FR-019**: The entire platform — Compose infrastructure **and** the native daemons — MUST come
  up and tear down from a single command each, with daemon IPs wired automatically (extends
  FR-013 to the hybrid-GPU services).
- **FR-020**: A bootstrap MUST provision the native execution environment (Python venv + pinned
  deps, required model/weight seeding, GPU build/availability check) idempotently and verify gate
  zero before serving/training is attempted.
- **FR-021**: The platform MUST be retargetable to a different machine by editing only
  `hardware-profile.md`; a portability check MUST take a clean clone to a passing serving smoke
  test.
- **FR-022**: Auth and secret errors MUST be clear and non-leaky; API keys and credentials MUST
  NOT appear in logs, metrics, or error bodies.

### Key Entities *(include if feature involves data)*

- **ApiKey**: a locally-configured gateway credential — a label and a secret (stored/compared as
  a hash), optionally a coarse scope (read vs. write). No user accounts, no RBAC.
- **Secret**: a required runtime credential (object-store, database, dashboard) sourced from a
  local secret file / environment, with no committed default value.
- **DaemonProcess**: a supervised native process — name, command, port, health URL, current
  state (starting / healthy / unhealthy / backoff), and restart count.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-008**: With auth enabled, every protected endpoint rejects a missing/invalid key (`401`)
  and accepts a valid key; `/healthz` and `/metrics` remain reachable unauthenticated.
- **SC-009**: No committed file contains a usable production secret; a fresh bring-up without
  configured secrets fails fast with guidance (no silent default fallback).
- **SC-010**: One command brings the full platform (infra + native daemons) to ready; a killed
  daemon is automatically restarted and returns to healthy within a bounded time.
- **SC-011**: On a clean environment, editing only `hardware-profile.md` and running the bootstrap
  reaches a passing serving smoke test with no manual fix-ups; re-running it is a no-op.
- **SC-012**: All 001 integration tests still pass with hardening enabled (no regression to
  US1–US5), given a valid API key.

## Assumptions

- **Single local operator, still** — auth is a shared API key (or a small local key set) to stop
  open access, NOT multi-tenant accounts, RBAC, SSO, or public internet exposure. TLS termination
  is out of scope for a localhost-bound platform (revisit only if exposed beyond the host).
- The hybrid-GPU model (constitution v1.2.0) stands: GPU/vision services remain native WSL
  processes; the supervisor manages them as host processes, introducing no new runtime.
- Secrets live in an untracked local file (e.g., `.env` / `secrets/`) ignored by git; rotating a
  key is editing that file and restarting the gateway.
- Portability targets another single machine matching the hardware-profile shape (WSL2 + NVIDIA);
  it does not imply multi-node or cloud.
- This increment changes no lifecycle behavior; it is purely operational hardening over 001.
