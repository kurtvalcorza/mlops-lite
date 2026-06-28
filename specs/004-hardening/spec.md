# Feature Specification: MLOps-Lite Operator-Console Hardening

**Feature Branch**: `004-hardening`

**Created**: 2026-06-28

**Status**: Draft

**Input**: User description: "Harden the 003 operator console the way 002 hardened the platform:
lock down the BFF so the server-side API key can't be ridden to arbitrary gateway routes or by
another local page, get the UI dependency stack off a known-vulnerable Next.js, give the console
real security headers; make the Node/UI tier part of the reproducible bootstrap + portability check
so a clean machine reaches a healthy UI without a manual build; and make the ui daemon resilient —
no crash-loop on a bad build, health that reflects real readiness."

> **Scope note**: 004 *extends* 003 (the operator console) the same way 002 extended 001 — it adds
> **no UI surface and no lifecycle behavior**. It lifts three v1 assumptions baked into the 003 MVP
> ("BFF trusts any same-host caller and proxies anything", "the Node/UI tier is provisioned by hand
> on first launch", "process-up == ready"). Requirement IDs continue the shared space (FR-032+,
> SC-019+, tasks T079+). No constitution amendment: 004 stays within v1.3.0's allowances (Node
> confined to `ui/`; the UI as a native localhost service) — see plan.md → Constitution Check.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Lock down the BFF and the console surface (Priority: P1)

The operator's gateway API key lives only in the BFF. Today the BFF is an **open proxy**: it forwards
*any* path/method under `/api/gw/...` to the gateway with the key attached, and it answers any caller
that can reach `127.0.0.1:3000`. A hardened BFF only proxies an **explicit allowlist** of the gateway
routes the console actually uses, rejects everything else, and refuses **cross-origin / non-localhost**
callers — so a stray browser tab or another local app can't ride the key. The console ships **security
headers** and runs on a Next.js free of the known advisory it currently builds on.

**Why this priority**: The key is the platform's one real secret on the client tier; an open
same-host proxy is the biggest residual risk after 003. Highest value, smallest surface — like 002 US1.

**Independent Test**: A request through the BFF to an allowlisted route+method succeeds with the key
injected; a request to a non-allowlisted gateway path (or a disallowed method) is rejected by the BFF
**without** forwarding the key; a request carrying a foreign `Origin` is rejected; the key never
appears in any browser-visible payload; `npm audit` reports no high/critical advisory for production
dependencies; every console route returns the security headers.

**Acceptance Scenarios**:

1. **Given** the allowlist, **When** the browser calls an allowlisted gateway route via the BFF,
   **Then** it succeeds exactly as in 003 (no behavior change to the six tabs).
2. **Given** the allowlist, **When** the browser requests a gateway path/method NOT on it,
   **Then** the BFF returns `404`/`405` and never forwards the API key upstream.
3. **Given** a request with a cross-origin `Origin` (or a non-localhost `Host`), **When** it hits the
   BFF, **Then** it is rejected (`403`) before any key injection.
4. **Given** any console route, **When** it is fetched, **Then** the response carries CSP,
   frame-ancestors deny, `X-Content-Type-Options: nosniff`, and a referrer policy; and Next.js is on
   a patched version (no CVE-2025-66478).

---

### User Story 2 — Reproducible Node/UI bootstrap & portability (Priority: P2)

A clean machine reaches a **healthy operator console** through the same idempotent bootstrap that
provisions the Python tier — no manual `npm` step, no first-launch build under the supervisor (which
can overrun the bring-up timeout and needs network). Bootstrap verifies a **Node gate** (a supported
runtime is present), runs `npm ci` and `next build` idempotently, and the **portability check**
covers the Node/UI prerequisites as part of the retarget contract.

**Why this priority**: 002 US4 made the *Python* tier portable; the 003 UI added a Node tier that
bootstrap and the portability check don't know about, so the "edit only the hardware profile" claim
is now incomplete. Depends on US1 being in place (bootstrap provisions a *hardened* console).

**Independent Test**: On a pruned clone (no `node_modules`, no `.next`), run the bootstrap; it
verifies Node, installs deps, and builds the UI. Then `up_all` brings the ui daemon to healthy with
**no** lazy build under the supervisor. Re-running the bootstrap is a no-op.

**Acceptance Scenarios**:

1. **Given** a clean checkout and a supported Node runtime, **When** the bootstrap runs, **Then** it
   verifies Node, runs `npm ci` + `next build`, and reports the UI ready.
2. **Given** a missing/old Node runtime, **When** the bootstrap runs, **Then** it fails fast with
   guidance (a Node gate, like Gate Zero for the GPU) rather than a confusing build error.
3. **Given** a UI provisioned by bootstrap, **When** `up_all` starts the daemons, **Then** the ui
   daemon reaches healthy promptly without building under the supervisor.
4. **Given** an already-provisioned machine, **When** the bootstrap is re-run, **Then** it is a no-op
   (deps present, build current).

---

### User Story 3 — Console readiness (Priority: P3) — trimmed to readiness only

The console's health reflects real **readiness** (the BFF can reach the gateway), not just that the
Node process is alive. (The crash-loop/orphan class is already solved — the process-group teardown fix
landed this session and 002's `MAX_RESTARTS` already bounds restarts — so US3 carries only the
readiness signal, not a redundant build-failure task.)

**Why this priority**: This session hit a silent crash-loop (orphaned `next-server` held the port;
the shallow liveness probe stayed green). The fix landed; what's still worth adding is the honest
"process up ≠ console functional" distinction. Lowest urgency; small and independent.

**Independent Test**: With the gateway up, `/readyz` reports ready; with the gateway unreachable,
`/readyz` reports not-ready while `/healthz` (liveness) stays up. The ui daemon's restart_count stays
stable (orphan fix holds).

**Acceptance Scenarios**:

1. **Given** the gateway is reachable, **When** `/readyz` is checked, **Then** it reports ready;
   **When** the gateway is unreachable, **Then** it reports not-ready — distinct from the cheap
   liveness `/healthz` the supervisor polls.
2. **Given** the orphan fix (process-group teardown), **When** the ui daemon is restarted, **Then**
   no `next-server` survives to hold the port (restart_count stays stable, no crash-loop).

---

### Edge Cases

- **Key never rides a non-console call**: a request to a gateway path the console doesn't use is
  refused by the BFF *before* the key is attached (FR-032).
- **Foreign origin**: a page on another localhost port / a foreign `Origin` cannot use the BFF as a
  confused-deputy to reach the gateway with the key (FR-033).
- **Vulnerable dependency**: the UI must not build on a Next.js version with a known high/critical
  advisory; the upgrade must not regress the six tabs (FR-034).
- **No Node on a new machine**: bootstrap fails fast with guidance (a Node gate), before the UI build
  (FR-037).
- **Bad UI build**: the ui daemon does not crash-loop; it backs off to persistent-unhealthy (FR-039).
- **Shallow health**: liveness (`/healthz`, what the supervisor polls) stays cheap; *readiness*
  (gateway reachable) is a separate signal so "process up" isn't mistaken for "console works" (FR-040).
- **No regression**: all 001/002/003 tests still pass; the six tabs behave identically (SC-024).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-032**: The BFF MUST proxy only an explicit allowlist of gateway routes (path patterns +
  methods) the console uses; any other path/method MUST be rejected (`404`/`405`) **without**
  forwarding the API key upstream. No open `[...path]` passthrough.
- **FR-033**: The BFF MUST reject cross-origin / non-localhost callers (validate `Origin`/`Host`
  against `127.0.0.1`/`localhost`) before injecting the key, so the server-side key cannot be used by
  another local page (confused-deputy / CSRF-style abuse). Same-origin console calls are unaffected.
- **FR-034**: The UI dependency stack MUST be free of known **high/critical** advisories at build —
  specifically off **CVE-2025-66478** (upgrade Next.js to a patched 15.x); `npm audit --omit=dev`
  MUST report no high/critical (or document an accepted exception). The upgrade MUST NOT change the
  six tabs' behavior.
- **FR-035**: The console MUST send security response headers on every route: a **pragmatic**
  Content-Security-Policy — `default-src 'self'`; `connect-src 'self'`; `script-src`/`style-src 'self'
  'unsafe-inline'` (Tailwind + Next hydration, no nonce plumbing); `frame-src`/`img-src` allow the
  configured Grafana origin for the Health iframe; **`frame-ancestors 'none'`** — plus
  `X-Content-Type-Options: nosniff` and a `Referrer-Policy`.
- **FR-036**: Grafana iframe embedding MUST be scoped so only the operator console origin
  (`http://localhost:3000`) may frame it — via Grafana's own CSP `frame-ancestors`
  (`GF_SECURITY_CONTENT_SECURITY_POLICY`) — rather than the current blanket allow-embedding that lets
  any site frame it.
- **FR-037**: The bootstrap MUST provision the Node/UI tier idempotently — verify a supported Node
  runtime (a **Node gate: ≥ 20 LTS**, fail-fast with guidance if absent/older), run `npm ci`, and
  `next build` — so `up_all` brings the ui daemon healthy with no build under the supervisor.
- **FR-038**: The portability contract MUST cover the Node/UI tier: the portability check verifies
  Node/npm presence and that the UI builds; `native_env.lock` (or an equivalent) records the Node
  version pin alongside the GPU wheel pins.
- **FR-039** *(already satisfied — no new task)*: The ui daemon MUST NOT crash-loop on a build/start
  failure — repeated failures back off to `persistent-unhealthy` (002's `MAX_RESTARTS` policy) and the
  process-group teardown leaves no orphaned `next-server`. This landed this session; US3 only adds a
  regression assertion (restart_count stable), not new behavior.
- **FR-040**: The console MUST expose a **readiness** signal distinct from liveness — `/healthz`
  stays a cheap liveness probe (what the supervisor polls), and `/readyz` reflects whether the BFF can
  reach the gateway — so "process up" is not mistaken for "console functional".

### Key Entities *(include if feature involves data)*

- **AllowedRoute**: one BFF-proxy entry — a gateway path pattern (e.g., `models`, `infer/stream`,
  `runs/:id/events`) and the method(s) permitted. The allowlist is the BFF's complete proxy surface.
- **NodeRuntime**: the bootstrap's Node gate target — a minimum supported version; recorded as a pin
  in the env lock for portability.
- **DaemonProcess** (extends 002): the ui daemon gains a *readiness* dimension (gateway-reachable)
  layered over its liveness state.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-019**: The BFF forwards the key only for allowlisted route+method; a non-allowlisted gateway
  path/method and a cross-origin/non-localhost request are rejected with no key forwarded.
- **SC-020**: The UI builds on a patched Next.js (no CVE-2025-66478) and `npm audit --omit=dev`
  reports zero high/critical advisories (or a documented exception); the six tabs are unchanged.
- **SC-021**: Every console route returns the required security headers (CSP with `frame-ancestors
  'none'`, nosniff, referrer policy); the Health iframe still renders.
- **SC-022**: On a pruned clone, bootstrap (Node gate → `npm ci` → `next build`) makes the UI ready;
  `up_all` then brings the ui daemon healthy with no supervisor-time build; re-running bootstrap is a
  no-op.
- **SC-023**: A forced UI build/start failure ends in bounded `persistent-unhealthy` (no infinite
  fast restart, no orphaned `next-server`); fixing it recovers to healthy; readiness reflects gateway
  reachability.
- **SC-024**: All 001/002/003 integration tests still pass (no regression to any lifecycle or UI
  behavior), with auth on and the hardened BFF in place.

## Assumptions

- **Single local operator, still** (002's assumption holds) — the BFF lockdown is defense-in-depth
  for a localhost-bound, no-login console, NOT multi-user auth, RBAC, or public exposure. No TLS for a
  127.0.0.1 surface.
- The hybrid model (constitution v1.2.0/v1.3.0) stands: Node is confined to `ui/`; the UI is a native
  localhost service. 004 introduces **no new runtime** and no constitution amendment.
- The CSP allows the configured Grafana origin only for the Health iframe; Grafana stays anonymous +
  localhost (002/003 boundary).
- Portability still targets another single WSL2 + NVIDIA machine; the Node gate is a version check,
  not a Node installer (prereq, like the NVIDIA driver).
- This increment changes no lifecycle behavior and adds no UI surface; it is purely security +
  operational hardening over 003.
