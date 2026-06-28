# Implementation Plan: MLOps-Lite Operator-Console Hardening

**Branch**: `004-hardening` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/004-hardening/spec.md`

## Summary

Harden the 003 operator console along three axes, adding no UI surface: (US1) lock the BFF to an
explicit gateway-route allowlist + a same-origin/localhost guard, upgrade Next.js off CVE-2025-66478,
and ship console security headers; (US2) fold the Node/UI tier into the idempotent `bootstrap.sh` +
the portability check (Node gate, `npm ci`, `next build`); (US3) make the ui daemon resilient — no
crash-loop on a bad build, a readiness signal distinct from liveness. Phase-gated like 002, validated
live each phase, never regressing 001/002/003.

## Technical Context

**Language/Version**: TypeScript on Node.js (Next.js 15, App Router) for the BFF/console changes;
Bash for `bootstrap.sh`/portability; Python (stdlib) for the supervisor + tests. No new languages.

**Primary Dependencies**: no new runtime deps. Next.js **15.1.7 → 15.5.19** (patched, same major —
minimal churn). Allowlist + origin guard + headers are plain Next route-handler / `next.config`
code. Grafana scoping is config only.

**Storage**: none. The BFF stays stateless; the key still comes from the environment (002's `.env`).

**Target Platform**: Win11 + WSL2. UI remains a native WSL Node process under the 002 supervisor;
bootstrap runs in WSL.

**Project Type**: operational + security hardening increment over 003 — touches `ui/` (BFF, config),
`scripts/bootstrap.sh`, the portability test, `supervisor/` (resilience), `docker-compose.yml`
(Grafana scoping), and tests. No gateway lifecycle change.

**Performance Goals**: the allowlist/origin guard add negligible per-request cost; pre-building the UI
in bootstrap removes the first-launch build from the bring-up critical path (faster, offline `up_all`).

**Constraints**: keep the key server-side (003 FR-024 preserved and tightened); localhost-only
(no TLS); no new always-on service; Principle II/III untouched; the six tabs must behave identically.

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | All changes localhost-only; no cloud/IdP added | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | 004 touches the console/BFF/bootstrap only; never the VRAM mutex | ✅ unchanged |
| III. Lightweight Footprint | No new image/service; UI stays native WSL; bootstrap reuses the existing flow | ✅ |
| IV. Full Lifecycle Coverage | No stage added/dropped — operational hardening over a surface | ✅ N/A |
| V. OSS & Swappable | Next.js upgrade is OSS→OSS; no new vendor | ✅ |
| VI. Reproducibility & Observability | Bootstrap now reproduces the Node tier too; readiness improves observability | ✅ strengthened |
| VII. Phase-Gated Delivery | Three independently-runnable stories (US1–US3), each verifiable on the target machine | ✅ |
| Workflow: "No other runtime without amendment" | Node already ratified (v1.3.0, confined to `ui/`); 004 adds none | ✅ no amendment |
| Workflow: native-host pattern | UI native-localhost service already allowed (v1.3.0) | ✅ |

**No amendment required.** 004 operates entirely within v1.3.0's allowances — unlike 003 (which
*introduced* the Node runtime and needed v1.3.0), 004 only hardens that already-ratified tier. The
constitution gate is PASS; this mirrors 002's clean gate-check over 001.

## Project Structure

### Documentation (this feature)

```text
specs/004-hardening/
├── plan.md     # This file
├── spec.md     # Feature specification
└── tasks.md    # Task list
```

### Source Code (delta over 003)

```text
mlops-lite/
├── ui/
│   ├── app/api/gw/[...path]/route.ts   # MODIFIED: allowlist + origin/Host guard (was open proxy)
│   ├── lib/gw-allowlist.ts             # NEW: the gateway-route allowlist (single source of truth)
│   ├── next.config.mjs                 # MODIFIED: security headers() (CSP, frame-ancestors, nosniff)
│   ├── package.json                    # MODIFIED: next 15.1.7 → 15.5.19
│   └── lib/sse.ts / app/.../route.ts   # (only if a path needs the allowlist — no behavior change)
├── scripts/
│   ├── bootstrap.sh                    # MODIFIED: Node gate + npm ci + next build (idempotent)
│   └── native_env.lock                 # MODIFIED: record the Node version pin
├── supervisor/supervise.py             # (resilience: confirm build-fail → persistent-unhealthy; the
│                                        #  process-group orphan fix already landed this session)
├── docker-compose.yml                  # MODIFIED: scope Grafana embedding (frame-ancestors/samesite)
└── tests/
    ├── test_ui_security.py             # EXTENDED: allowlist enforced, cross-origin rejected, headers
    ├── test_portability.py             # EXTENDED: Node gate + UI builds (retarget contract)
    └── test_ui_resilience.py           # NEW: build-fail → persistent-unhealthy; readiness signal
```

**Structure Decision**: Security work is confined to the BFF + console config (one allowlist module
is the BFF's whole proxy surface); the bootstrap/portability work extends the existing 002 scripts;
resilience is a small supervisor/run.sh confirmation plus a test. Nothing in the gateway or the
lifecycle daemons changes.

## Phasing (maps to constitution VII)

- **Phase 1 — BFF & console lockdown (US1)**: allowlist + origin/Host guard in the BFF; Next upgrade
  off the CVE; security headers; scope Grafana embedding. Exit: non-allowlisted/cross-origin calls
  refused with no key forwarded; headers present; six tabs unchanged; `npm audit` clean.
- **Phase 2 — Node/UI bootstrap & portability (US2)**: `bootstrap.sh` Node gate + `npm ci` +
  `next build`; portability check covers Node/UI; lock the Node pin. Exit: pruned clone → bootstrap →
  `up_all` brings ui healthy with no supervisor-time build; re-run is a no-op.
- **Phase 3 — console readiness (US3, trimmed)**: add a `/readyz` readiness signal distinct from
  liveness (the crash-loop/orphan class is already solved — landed this session + 002's MAX_RESTARTS).
  Exit: `/readyz` reflects gateway reachability; restart_count stays stable.

Cross-cutting: a no-regression pass of the full 001/002/003 suite with the hardened BFF + auth on.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| BFF route allowlist (not open `[...path]`) | The key is attached server-side to whatever path the browser asks — an open proxy lets any local page reach any gateway route with the operator's key | Keeping the open proxy keeps the confused-deputy hole; a gateway-side per-route exemption would re-open 003's key-in-client problem |
| Origin/Host guard on the BFF | Stops another localhost app/tab from using the BFF as a confused deputy | CORS alone doesn't stop a server-side proxy; the guard must be server-side |
| Next 15.1.7 → 15.5.19 (same major) | Patches CVE-2025-66478 with minimal churn; App Router API stable across 15.x | A major bump (16.x) risks breaking the six tabs for no extra security; staying on 15.x is the low-risk patch |
| Node tier in bootstrap (not lazy in run.sh) | Lazy build under the supervisor can overrun the bring-up timeout and needs network; bootstrap is the reproducible, offline-after-pull provisioning step | Lazy first-launch build is what 003 shipped — it's the operational gap 004 closes |
| Readiness ≠ liveness | A shallow process-up probe masked this session's crash-loop; readiness (gateway reachable) is the honest "console works" signal | A single deeper `/healthz` would make the supervisor's liveness poll do a gateway round-trip on every tick — keep liveness cheap |
