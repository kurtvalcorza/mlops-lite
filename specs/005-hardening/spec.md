# Feature Specification: MLOps-Lite Audit Hardening

**Feature Branch**: `005-hardening`

**Created**: 2026-06-28

**Status**: Draft

**Input**: External code-review/audit (Codex, 2026-06-28) of the gateway auth, UI BFF, Docker Compose
exposure, operator CLIs, and test posture. This feature closes the legitimate findings.

> **Scope note**: 005 *extends* the hardened platform (002/004) — it adds **no lifecycle behavior and
> no UI surface**. It closes audit findings about network exposure, auth defaults, CLI auth parity,
> and BFF robustness. Requirement IDs continue the shared space (FR-041+, SC-025+, tasks T093+).
> **No constitution amendment** — binding services to loopback *strengthens* Principle I
> (local-first); nothing introduces a new runtime (see plan.md → Constitution Check).

> **Audit triage (what's in / out).** Of the 8 findings, 005 acts on the valid ones and records the
> rest:
> - **In:** #1 ports on `0.0.0.0` (High), #2 auth-open default (High, narrowed — see US2), #3 CLIs
>   missing the key (Med), #4 BFF origin rigidity (Med), #5 BFF error topology leak (Med), #6 static
>   key + #8 pytest-collects-nothing (Low, doc-level).
> - **Out (accepted / not a defect):** #7 CSP `unsafe-inline` — the deliberate 004 grilled decision
>   for a localhost single-operator console (XSS cannot read the server-side key); revisit only if the
>   console is ever exposed beyond the host. See Non-Goals.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Bind all services to loopback (Priority: P1)

Every platform service is reachable **only from the host's loopback**, not the LAN. The Compose stack
publishes Postgres, MinIO (API + console), MLflow, the gateway, Prometheus, and Grafana — none of which
should answer on a routable interface for a single-operator localhost platform (Grafana is anonymous;
MLflow/Prometheus aren't behind the gateway key).

**Why this priority**: Network exposure is the highest real risk in the audit — it's the difference
between "localhost tool" and "anyone on the coffee-shop Wi-Fi can hit MLflow/Grafana/MinIO." Matches
the platform's already-stated localhost posture (the UI binds `127.0.0.1`; the infra didn't).

**Independent Test**: After bring-up, each published port answers on `127.0.0.1` but **not** on the
host's LAN IP. The WSL native daemons still reach MinIO/MLflow over `localhost`, and the browser/BFF
still reach the gateway/Grafana/Prometheus — i.e. the loopback bind doesn't break the platform.

**Acceptance Scenarios**:

1. **Given** the stack is up, **When** a port (gateway/MLflow/Grafana/MinIO/Prometheus/Postgres) is
   probed on the host LAN IP, **Then** the connection is refused; **When** probed on `127.0.0.1`,
   **Then** it answers.
2. **Given** loopback binding, **When** the training/vision daemons (native WSL) start, **Then** they
   still reach MinIO (`:9000`) and MLflow (`:5500`) over `localhost` (no regression).
3. **Given** an operator who genuinely needs LAN exposure, **When** they set a documented override
   (`BIND_ADDR`), **Then** the bind address changes — exposure is explicit, never the default.

---

### User Story 2 — Fail-closed gateway auth by default (Priority: P1)

The gateway does **not** silently serve protected routes unauthenticated. Today, no configured keys ⇒
open mode with a log warning. Hardened to: **no keys ⇒ refuse protected access** unless an operator
sets an explicit development override; the override logs a prominent warning. The provisioned path
(`gen_secrets` → `up_all`) is unchanged — it always has a key, so it's always authenticated.

**Why this priority**: Defense-in-depth behind US1. Even loopback-only, an open lifecycle API
(promote/launch-run/retrain/datasets) shouldn't be the default. (Narrowing the audit's framing: the
compose `${VAR:?}` secret-guards already block a *bare* `docker compose up` without `.env`, and
`gen_secrets` always writes a key — so the realistic open path is an operator explicitly emptying
`GATEWAY_API_KEYS`. US2 makes even that fail closed unless they opt in.)

**Independent Test**: With no keys and no override, a protected route is refused (and/or the gateway
refuses to start) with clear guidance. With the explicit dev override, it runs open and logs the
warning. With a configured key, behavior is exactly as today.

**Acceptance Scenarios**:

1. **Given** no `GATEWAY_API_KEYS` and no override, **When** a protected route is called, **Then** it
   is refused (closed by default) with guidance to configure a key or set the dev override.
2. **Given** the explicit dev override (`GATEWAY_ALLOW_OPEN=1`), **When** the gateway starts, **Then**
   it runs open and logs a prominent warning (the documented escape hatch).
3. **Given** a configured key (the provisioned path), **When** routes are called, **Then** auth
   behaves exactly as in 002 (no regression).

---

### User Story 3 — Operator CLIs work against the hardened gateway (Priority: P2)

The provided operator CLIs (dataset registration, drift check) send the API key, so they work against
an auth-on gateway instead of only against an open one.

**Why this priority**: A documented-as-hardened platform whose own CLIs can't talk to the hardened
gateway is an operational trap. Small, contained fix (a shared header helper already exists for tests).

**Independent Test**: With auth enabled and a key available (env/file/flag), `register_dataset.py` and
`drift.py` succeed; with no key (open dev mode) they behave as before.

**Acceptance Scenarios**:

1. **Given** auth is enabled and `GATEWAY_API_KEY` (or `GATEWAY_API_KEYS_FILE` / `--api-key`) is set,
   **When** the dataset/drift CLI runs, **Then** it includes `X-API-Key` and succeeds.
2. **Given** no key configured, **When** the CLIs run against an open gateway, **Then** they work
   unchanged (the key header is simply omitted).

---

### User Story 4 — BFF robustness: configurable origins + non-leaky errors (Priority: P3)

The BFF keeps its strict localhost guard but (a) allows the loopback set to be **extended via
configuration** (and includes IPv6 loopback by default), and (b) does **not leak the upstream gateway
URL** in browser-visible error bodies.

**Why this priority**: Quality/robustness, not a live exploit. Keeps the strong default while removing
two sharp edges the audit flagged (over-rigid host matching; topology in errors).

**Independent Test**: The BFF still rejects a foreign `Origin` by default; a configured extra origin
is accepted; an unreachable-gateway error returns a generic message with no upstream URL/host/port.

**Acceptance Scenarios**:

1. **Given** the default config, **When** a request carries a foreign `Origin`, **Then** it is
   rejected `403` (unchanged); **When** it carries `[::1]`/`localhost`, **Then** it is accepted.
2. **Given** `UI_ALLOWED_ORIGINS` is set, **When** a request from a listed origin arrives, **Then** it
   is accepted; others are still rejected.
3. **Given** the gateway is unreachable, **When** the BFF responds, **Then** the client body is a
   generic error (e.g. `{"error":"gateway unreachable"}`) and the upstream URL is only in server logs.

---

### User Story 5 — Test-runner conversion + rotation docs (Priority: P3)

Make `pytest` the real entry point: **convert the `tests/` integration scripts to native pytest
tests** (each `main()` check → `test_*` with `pytest.skip` guards when the stack/key/WSL is absent),
preserving their pass/skip semantics. Also document the (by-design) **key-rotation-needs-restart**
behavior.

**Why this priority**: Lowest urgency, but the chosen path (full convert) gives a single working
`pytest` command and unifies the no-regression runs. No production behavior change.

**Independent Test**: `pytest` runs the suite — passing against a live, keyed stack and **skipping
cleanly** (not failing/erroring) when offline; no misleading exit-5. The rotation-needs-restart
behavior is documented in code + README.

**Acceptance Scenarios**:

1. **Given** a live keyed stack, **When** `pytest` runs, **Then** the converted integration tests
   execute and pass (same assertions as the old scripts).
2. **Given** no stack / no key, **When** `pytest` runs, **Then** the tests **skip** cleanly (not
   collect-nothing exit-5, not hard failures).
3. **Given** an operator rotates a key, **When** they read the auth code/README, **Then** the
   "restart the gateway to take effect" behavior is documented.

---

### Edge Cases

- **Loopback breaks WSL daemons**: if a `127.0.0.1` bind made MinIO/MLflow unreachable from the WSL
  daemons, that's a regression — US1's test gates on daemon reachability (FR-041).
- **Open override must be loud**: the dev escape hatch must log a prominent warning so it's never
  silently on (FR-042).
- **CLI with no key vs hardened gateway**: a CLI run without a key against an auth-on gateway should
  fail clearly (401), not hang or leak (FR-043).
- **Configured origin typo**: a malformed `UI_ALLOWED_ORIGINS` entry must not widen the guard to
  allow-all (FR-044).
- **No regression**: all 001–004 tests still pass; the six tabs behave identically (SC-030).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-041**: All Compose-published service ports (Postgres, MinIO API+console, MLflow, gateway,
  Prometheus, Grafana) MUST bind to loopback (`127.0.0.1`) by default; a documented override
  (`BIND_ADDR`) MAY change it. WSL native daemons MUST retain access to MinIO/MLflow — and if loopback
  publishing breaks their `localhost` path under Rancher, the **daemon side** MUST be adjusted (host/
  WSL-gateway IP or mirrored networking) so MinIO/MLflow are **never** left on a routable interface
  (the loopback default is not weakened to restore daemon access). *(Grilled decision, 2026-06-28.)*
- **FR-042**: The gateway MUST be fail-closed by default: with no configured keys it **boots** (so
  `/healthz` + `/metrics` stay up for probes) but **refuses protected lifecycle routes** with a clear
  `401` (guidance: set `GATEWAY_API_KEYS` or `GATEWAY_ALLOW_OPEN`), UNLESS the explicit dev override
  `GATEWAY_ALLOW_OPEN` (truthy) is set — which runs open and logs a **prominent warning**. The
  provisioned (keyed) path is unchanged. *(Grilled decision: refuse-routes, not refuse-start.)*
- **FR-043**: Operator CLIs calling protected routes MUST attach `X-API-Key` when a key is available
  (`GATEWAY_API_KEY` / `GATEWAY_API_KEYS_FILE` / `--api-key`), consistent with `tests/_auth.py`; with
  no key they behave as before.
- **FR-044**: The BFF's allowed Host/Origin set MUST be configurable (`UI_ALLOWED_ORIGINS`) while
  defaulting to localhost-only (`127.0.0.1`, `localhost`, `[::1]` on the UI port); cross-origin is
  still rejected and a malformed config MUST NOT widen to allow-all.
- **FR-045**: BFF error responses MUST NOT expose the internal upstream URL/host/port to the client;
  the detail is logged server-side and the client receives a generic message.
- **FR-046**: Key-rotation semantics (rotation requires a gateway restart) MUST be documented at the
  code and README level. (Reload-on-change is OPTIONAL and out of scope unless trivial.)
- **FR-047**: The `tests/` integration scripts MUST be **converted to native `pytest` tests** — each
  script's `main()` check becomes `test_*` function(s) that assert, with `pytest.skip(...)` guards when
  prerequisites (live stack, `GATEWAY_API_KEY`, WSL) are absent — so `pytest` is a working entry point
  that runs the suite and skips cleanly offline (no misleading exit-5). Existing pass/skip semantics
  MUST be preserved. *(Grilled decision: full convert, not exclude-and-document.)*

### Key Entities *(include if feature involves data)*

- **BindAddress**: the host interface Compose publishes to — default `127.0.0.1`, overridable via
  `BIND_ADDR` for intentional exposure.
- **AuthMode**: derived gateway posture — `keyed` (provisioned), `closed` (no key, default), or
  `open-override` (no key + explicit `GATEWAY_ALLOW_OPEN`, warns loudly).
- **AllowedOrigin** (extends 004): the BFF's accepted Host/Origin set — localhost defaults plus an
  optional configured list.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-025**: By default no platform service answers on a non-loopback interface; the WSL daemons,
  browser, and BFF still reach their services (no regression).
- **SC-026**: With no key and no override, the gateway boots (probes up) but protected lifecycle
  routes return `401` with guidance; `GATEWAY_ALLOW_OPEN` runs open with a prominent warning; the
  provisioned keyed path is unchanged.
- **SC-027**: The dataset and drift CLIs succeed against an auth-on gateway with a provided key, and
  still work in open mode.
- **SC-028**: The BFF rejects foreign origins by default, accepts a configured extra origin, and
  returns no upstream URL in client-visible errors.
- **SC-029**: `pytest` runs the converted suite — passing against a live keyed stack and skipping
  cleanly offline (no exit-5); rotation-needs-restart is documented.
- **SC-030**: All 001/002/003/004 integration tests still pass (no regression to any lifecycle or UI
  behavior), with auth on and the loopback binds in place.

## Assumptions

- **Single local operator, still** — 005 hardens a localhost-bound platform; it does NOT add
  multi-user auth, RBAC, SSO, TLS, or LAN/internet exposure (LAN exposure is the thing US1 closes).
- The hybrid model (constitution v1.2.0/v1.3.0) stands; Node stays confined to `ui/`; no new runtime,
  no amendment.
- WSL2 forwards `localhost` to host-loopback-published ports (the same forwarding the BFF→gateway and
  daemon→MLflow/MinIO paths already rely on) — US1 verifies this rather than assuming it.
- This increment changes no lifecycle behavior and adds no UI surface; it is purely security +
  operational hardening over 002/004.

## Non-Goals

- **CSP nonce/hash migration (audit #7)** — the pragmatic `unsafe-inline` CSP is the deliberate 004
  decision for a localhost single-operator console; out of scope unless the console is exposed beyond
  the host.
- **Per-request key hot-reload (audit #6 beyond docs)** — documented as restart-required; live reload
  is optional and not pursued here.
- **Multi-user accounts / RBAC / TLS / public exposure** — unchanged single-operator posture.
