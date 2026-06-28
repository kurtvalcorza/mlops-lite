---
description: "Task list for MLOps-Lite Operator-Console Hardening (004)"
---

# Tasks: MLOps-Lite Operator-Console Hardening

**Input**: Design documents from `specs/004-hardening/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the feature-complete (001) +
hardened (002) + operator-console (003) platform.

**Tests**: Lightweight per-phase smoke/integration tests (constitution VII), run on the target
machine before the next phase. Task IDs continue the shared space (T079+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** ✅ **COMPLETE & validated on hardware.** All phases (1–4) built + verified
> live; 12-test no-regression sweep green (001/002/003 + 004). 004 hardened the 003 console; it changed
> no UI surface and no lifecycle behavior. **No constitution amendment** — stayed within v1.3.0's
> Node/native-localhost allowances. The 002-supervisor process-group orphan fix landed this session.
>
> **Grilled decisions (2026-06-28):** (1) **threat model = malicious-web-page CSRF** → US1 keeps BOTH
> the allowlist and the origin/Host guard at full strength (a foreign tab POSTing promote/launch/retrain
> is the confused-deputy we block); (2) **US3 trimmed to readiness only** — the orphan/crash-loop fix
> already landed + MAX_RESTARTS already bounds restarts, so US3 = `/readyz` + its test (dropped the
> redundant build-fail task); (3) **CSP = pragmatic** (self + Grafana origin, `unsafe-inline` for
> Tailwind/Next hydration, `frame-ancestors 'none'` — no nonce plumbing); (4) **Next → 15.5.19**
> (latest 15.x, same major) + committed lockfile; (5) **Grafana embedding scoped** to the console
> origin (`frame-ancestors http://localhost:3000`, not blanket); (6) **Node gate floor = ≥ 20 LTS**.

## Phase 1: BFF & Console Lockdown (US1, P1)

**Goal**: The server-side key serves only the console's own allowlisted calls, from the console only,
over a Next.js free of the known advisory, with security headers.

- [x] T079 [US1] `ui/lib/gw-allowlist.ts`: the single source of truth for the BFF's proxy surface —
  an explicit list of `{ method, pattern }` for every gateway route the six tabs use (`GET models`,
  `GET models/:name`, `POST models/:name/promote`, `POST infer/stream`, `POST vision/classify`,
  `GET/POST datasets`, `GET datasets/:name`, `POST runs`, `GET runs/:id/events`, `GET platform/health`,
  `GET platform/events`, `POST monitor/check`, `GET monitor`). A matcher for path patterns (`:id`).
- [x] T080 [US1] `ui/app/api/gw/[...path]/route.ts`: enforce the allowlist — reject any path/method
  not on it with `404`/`405` **before** injecting `X-API-Key` (the key never rides a non-console
  call); keep the SSE/REST passthrough for allowlisted routes unchanged (FR-032).
- [x] T081 [US1] Same-origin / localhost guard in the BFF: validate `Origin` (when present) and `Host`
  against `127.0.0.1`/`localhost`; reject cross-origin/foreign-host callers with `403` before key
  injection (confused-deputy / CSRF defense). Same-origin console calls unaffected (FR-033).
- [x] T082 [P] [US1] Upgrade `ui/package.json` next `15.1.7 → 15.5.19` (latest 15.x, same major);
  `npm install`, **commit the updated `package-lock.json`** (reproducible `npm ci` in US2) + rebuild;
  `npm audit --omit=dev` → zero high/critical (document any accepted exception). Smoke all six tabs —
  no behavior change (FR-034/SC-020).
- [x] T083 [US1] **Pragmatic** security headers via `next.config.mjs` `headers()`: CSP
  `default-src 'self'`; `script-src 'self' 'unsafe-inline'` + `style-src 'self' 'unsafe-inline'`
  (Tailwind + Next hydration — no nonce plumbing); `connect-src 'self'`; `frame-src`/`img-src` allow
  the configured Grafana origin for the Health iframe; **`frame-ancestors 'none'`** (the console isn't
  framable); plus `X-Content-Type-Options: nosniff` + a `Referrer-Policy` (FR-035).
- [x] T084 [P] [US1] Scope Grafana embedding in `docker-compose.yml`: keep `GF_SECURITY_ALLOW_EMBEDDING`
  but set `GF_SECURITY_CONTENT_SECURITY_POLICY: "true"` + a CSP whose `frame-ancestors` is limited to
  **`http://localhost:3000`** (the console origin) — so Grafana is framable ONLY by the console, not
  any site. Verify the Health iframe still renders (FR-036).
- [x] T085 [US1] Extend `tests/test_ui_security.py`: (a) allowlisted route+method through the BFF →
  `200`; (b) a non-allowlisted gateway path/method → `404`/`405`, key NOT forwarded (assert upstream
  never saw it); (c) a request with a foreign `Origin` → `403`; (d) every console route returns the
  required headers; (e) key still absent from all browser payloads (003 SC-014 preserved).
  **Checkpoint**: key serves only the console's own calls; headers present; CVE gone.

---

## Phase 2: Node/UI Bootstrap & Portability (US2, P2)

**Goal**: A clean machine reaches a healthy console through the idempotent bootstrap — no manual npm,
no first-launch build under the supervisor.

- [x] T086 [US2] `scripts/bootstrap.sh`: add a **Node gate** (verify `node`/`npm` present and
  **Node ≥ 20 LTS**, fail-fast with guidance if absent/older — the Node analogue of Gate Zero), then
  idempotently `cd ui && npm ci` (skip if `node_modules` current) and `npm run build` (skip if
  `.next` current). Re-run = no-op (FR-037/SC-022).
- [x] T087 [P] [US2] Record the Node version pin in `scripts/native_env.lock` (alongside the GPU
  wheel pins) so the retarget contract captures the Node tier (FR-038).
- [x] T088 [US2] `ui/run.sh`: now that bootstrap pre-builds, run.sh **fails fast** if `.next` is
  missing (point to bootstrap) instead of silently building under the supervisor; keeps the warm
  `next start` path (ties to FR-037 + US3 resilience).
- [x] T089 [US2] Extend `tests/test_portability.py`: the retarget contract verifies Node/npm presence
  and that `ui/.next` exists (UI built); optionally a headless `next build` dry-check. Asserts the
  "edit only hardware-profile.md" claim now covers the Node tier (FR-038/SC-022).
  **Checkpoint**: pruned clone → bootstrap → `up_all` → ui healthy with no supervisor-time build.

---

## Phase 3: Console Readiness (US3, P3) — trimmed to readiness only

**Goal**: Readiness ≠ liveness. (Build-fail/crash-loop is already solved — the process-group orphan
fix landed this session and 002's `MAX_RESTARTS` already bounds restarts — so no redundant task here.)

- [x] T090 [US3] Console **readiness** signal distinct from liveness: add `ui/app/readyz/route.ts`
  that checks the BFF can reach the gateway (`/healthz` upstream) and returns ready/not-ready; keep
  `/healthz` a cheap liveness probe (what the supervisor polls). Surface readiness in the Health tab's
  "this console" tile (FR-040).
- [x] T091 [US3] `tests/test_ui_resilience.py`: `/readyz` reflects gateway reachability — ready when
  the gateway is up; not-ready when it's unreachable; `/healthz` stays up regardless (liveness vs
  readiness are distinct). Also assert restart_count stays stable (orphan fix holds, no crash-loop).
  **Checkpoint**: honest readiness, distinct from liveness.

---

## Phase 4: Cross-Cutting Regression

- [x] T092 No-regression: the full 001/002/003 suite passes with the hardened BFF + auth on
  (auth/serving/registry/datasets/bento/stream/ui_smoke/ui_security), the six tabs behave identically,
  and `up_all`/`down_all` still bring the platform (incl. ui) clean (SC-024).

---

## Dependencies & Execution Order

- **US1 (BFF/console lockdown)** is the highest-value, lowest-surface security hardening — do first.
- **US2 (bootstrap/portability)** depends on US1 (bootstrap provisions a *hardened* console); it
  removes the first-launch build that US3 then no longer has to tolerate.
- **US3 (readiness)** is small and independent — just the `/readyz` signal + its test (the resilience
  work it would have done is already landed).
- Cross-cutting regression (T092) lands last.

### Constitution gates (re-check each phase)
- Principle II unchanged: 004 touches console/BFF/bootstrap only — never the VRAM mutex.
- Principle III: no new image/service; UI stays native WSL; bootstrap reuses the existing flow.
- No new runtime → no amendment (Node already ratified in v1.3.0, confined to `ui/`).
- 003 key-hygiene (FR-024) preserved and tightened (the key now serves only allowlisted console calls).

## Implementation Strategy

1. **US1 first** → lock the BFF, kill the CVE, add headers. 003 six tabs still green. **Stop and validate.**
2. **US2** → fold the Node tier into bootstrap + portability; prove a clean machine reaches a healthy console.
3. **US3** → honest failure + readiness.
4. Each phase ends with its test passing on the target machine; never regress 001/002/003.
