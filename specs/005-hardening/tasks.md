---
description: "Task list for MLOps-Lite Audit Hardening (005)"
---

# Tasks: MLOps-Lite Audit Hardening

**Input**: Design documents from `specs/005-hardening/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened platform (002/004).
Closes the 2026-06-28 Codex audit.

**Tests**: Lightweight per-phase smoke/integration tests (constitution VII), run on the target
machine before the next phase. Task IDs continue the shared space (T093+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **COMPLETE & VALIDATED ON HARDWARE.** All of US1–US5 implemented
> (T093–T103) and validated live after `up_all.ps1` recreated the stack with loopback binds + a
> rebuilt gateway + a rebuilt UI.
> - **US1/SC-025:** all 7 ports now publish on `127.0.0.1` (was `0.0.0.0`); LAN IP `192.168.0.212`
>   **refused** (`test_exposure` PASS); **all 4 WSL daemons healthy** on the loopback binds — the
>   Rancher daemon→MinIO/MLflow reachability risk did NOT materialize (no daemon-side fix needed).
> - **US2/SC-026:** `test_auth_modes` (14 cases) PASS — closed/open-override/keyed + truthy/falsy +
>   keyed-wins; live `test_auth` PASS.
> - **US3/SC-027:** `test_cli_auth` PASS (dataset+drift CLIs authenticate; 401 cleanly without a key).
> - **US4/SC-028:** `test_ui_security` PASS incl. `[::1]` accept; WSL-validated non-leaky `502
>   {"error":"gateway unreachable"}` + a throwaway BFF proved a **configured** `UI_ALLOWED_ORIGINS`
>   origin → 200 while foreign → 403 (malformed doesn't widen).
> - **US5/SC-029:** `pytest` is the entry point — offline run skipped cleanly (no exit-5); keyed run
>   green.
> - **SC-030/T102 no-regression:** keyed sweep **28 passed, 1 skipped** (supervisor, off-WSL);
>   `test_ui_smoke` PASS in WSL (six surfaces + live kill-flip). `test_finetune` deselected from the
>   sweep (multi-minute LoRA train, untouched by 005 — validated in phase 6).
> - **Bug found & fixed during live validation:** `test_ui_security._check_nonleaky_error` polled the
>   booting throwaway `next` via `_req` (catches only `HTTPError`) → connection-refused crashed it in
>   WSL; wrapped the poll in `try/except`. Test-only fix.
>
> **Build deviations from the draft (recorded):**
> - **T096** landed as a NEW unit module `tests/test_auth_modes.py` (not an extension of the live
>   `test_auth.py`): the no-key boot modes (closed / open-override) can't be exercised against one
>   running keyed gateway, so they're tested by reloading `auth.py` under different env — runs
>   offline, `importorskip('fastapi')`. The live `test_auth.py` keeps its keyed integration role.
> - **US5 guards:** the protected-route wrappers also require `GATEWAY_API_KEY` (a documented skip
>   prerequisite, FR-047) so a keyless run against the now-keyed-by-default gateway **skips** instead
>   of 401-failing. `test_supervisor` task ID stays T101's conversion; no separate T-id needed.
>
> 005 closes the Codex audit's valid findings; no lifecycle/UI change. **No constitution amendment** —
> loopback binding *strengthens* Principle I. Audit #7 (CSP `unsafe-inline`) is **out of scope** (the
> 004 decision; spec.md → Non-Goals). Tasks T093–T103.
>
> **Grilled decisions (2026-06-28):** (1) **US2 = refuse-routes** (gateway boots, `/healthz`+`/metrics`
> open, protected routes `401` with guidance) + `GATEWAY_ALLOW_OPEN` dev override — NOT refuse-start,
> and US2 is kept (not dropped); (2) **US1 = keep loopback, fix the daemon side** if Rancher breaks the
> WSL `localhost` path — MinIO/MLflow are never re-exposed; (3) **US5 = full-convert** all `tests/`
> integration scripts to native pytest tests w/ skip guards (preserve pass/skip semantics), so
> `pytest` is the entry point + the no-regression runner.

## Phase 1: Loopback Binding (US1, P1)

**Goal**: No platform service answers on a non-loopback interface by default.

- [x] T093 [US1] `docker-compose.yml`: prefix all seven published ports with `${BIND_ADDR:-127.0.0.1}:`
  — Postgres, MinIO API+console, MLflow, gateway, Prometheus, Grafana — so they bind loopback by
  default; `.env.example` documents `BIND_ADDR` (set to `0.0.0.0` only for intentional LAN exposure)
  (FR-041).
- [x] T094 [US1] Test `tests/test_exposure.py`: after bring-up, each port answers on `127.0.0.1` but
  is **refused on the host LAN IP**; AND the WSL daemons still reach MinIO (`:9000`) + MLflow (`:5500`)
  and the gateway resolves all daemons. **If the loopback publish breaks the daemons' `localhost`
  path** (Rancher-specific), **fix the daemon side** — point training/bento at the host/WSL-gateway IP
  (or enable WSL mirrored networking) so MinIO/MLflow stay loopback-only; do NOT re-expose them.
  *(Grilled: keep loopback, fix the daemon side.)* **Checkpoint**: LAN-closed, daemons intact (SC-025).

---

## Phase 2: Fail-Closed Gateway Auth (US2, P1)

**Goal**: No key configured ⇒ protected routes closed by default, unless an explicit dev override.

- [x] T095 [US2] `gateway/app/auth.py`: when no keys are configured, default **closed-routes** — the
  gateway still boots (`/healthz`, `/metrics`, `/` stay open for probes) but `require_api_key` returns
  `401` with guidance (set `GATEWAY_API_KEYS` or `GATEWAY_ALLOW_OPEN`) on protected routes — UNLESS
  `GATEWAY_ALLOW_OPEN` (truthy) is set, in which case run open and log a **prominent warning**. The
  keyed/provisioned path (`gen_secrets`/`up_all`) is unchanged. Plumb `GATEWAY_ALLOW_OPEN` in
  `docker-compose.yml` + `.env.example`; log the resolved auth-mode at startup. *(Grilled:
  refuse-routes, not refuse-start.)* (FR-042)
- [x] T096 [US2] Extend `tests/test_auth.py`: (a) no key + no override → `/healthz` `200` but a
  protected route `401` (closed-routes default); (b) no key + `GATEWAY_ALLOW_OPEN=1` → protected route
  open, warning logged; (c) valid key → `200` exactly as 002. **Checkpoint**: secure-by-default, dev
  override explicit (SC-026).

---

## Phase 3: Operator-CLI Auth Parity (US3, P2)

**Goal**: The shipped CLIs work against the hardened gateway.

- [x] T097 [P] [US3] `data/register_dataset.py` + `monitoring/drift.py`: attach `X-API-Key` when a key
  is available via `--api-key`, `GATEWAY_API_KEY`, or `GATEWAY_API_KEYS_FILE` (mirror the
  `tests/_auth.py` resolution); omit it (unchanged) when no key is set (FR-043).
- [x] T098 [US3] Test `tests/test_cli_auth.py`: with auth on + a key, the dataset and drift CLIs
  succeed (register a tiny dataset, run a drift check); without a key against an auth-on gateway they
  fail clearly (`401`). **Checkpoint**: CLIs match the hardened gateway (SC-027).

---

## Phase 4: BFF Robustness (US4, P3)

**Goal**: Strict-by-default but configurable origins; no topology in client errors.

- [x] T099 [US4] `ui/app/api/gw/[...path]/route.ts`: (a) add IPv6 loopback (`[::1]:UI_PORT`) to the
  default allowed set and make the set extendable via `UI_ALLOWED_ORIGINS` (comma-sep), keeping
  cross-origin rejected and a malformed entry from widening to allow-all; (b) replace the
  `gateway unreachable at ${GATEWAY_URL}` client body with a generic `{"error":"gateway unreachable"}`
  and `console.error` the detail server-side (FR-044/FR-045).
- [x] T100 [US4] Extend `tests/test_ui_security.py`: a configured extra origin is accepted while a
  foreign `Origin` is still `403`; the unreachable-gateway error body contains no `http://`/host/port.
  **Checkpoint**: robust guard, non-leaky errors (SC-028).

---

## Phase 5: pytest Conversion + Rotation Docs (US5, P3)

**Goal**: `pytest` is the working entry point — runs the suite live, skips cleanly offline.

- [x] T101 [US5] **Convert the `tests/` integration scripts to native pytest tests** (`test_auth`,
  `test_serving`, `test_registry`, `test_datasets`, `test_bento`, `test_foundation`, `test_offline`,
  `test_stream`, `test_ui_smoke`, `test_ui_security`, `test_ui_resilience`, `test_portability`,
  `test_supervisor`, `test_finetune`, `test_drift_loop`, + new 005 tests): each `main()` check becomes
  `test_*` function(s) that assert, with a shared `conftest.py` providing fixtures (gateway URL, key)
  and `pytest.skip(...)` when the stack/key/WSL is absent — **preserving the existing pass/skip
  semantics** (no assertion changes, just structure). `pyproject.toml` stays `testpaths=["tests"]`.
  *(Grilled: full convert.)* (FR-047)
- [x] T103 [P] [US5] Document **key rotation requires a gateway restart** in `gateway/app/auth.py`
  (comment) + README (FR-046). **Checkpoint**: `pytest` runs/skips cleanly; rotation documented (SC-029).

---

## Phase 6: Cross-Cutting Regression

- [x] T102 No-regression: **`pytest`** (converted suite) passes against a live keyed stack with the
  loopback binds + auth on; the six tabs behave identically; `up_all`/`down_all` still bring the
  platform (incl. ui) clean (SC-030).

---

## Dependencies & Execution Order

- **US1 (loopback)** is the highest-value, smallest-surface fix — do first; it also reduces US2's
  exploitability (open mode, if ever used, is then localhost-only).
- **US2 (fail-closed)** is independent of US1 but conceptually pairs with it (network + auth defaults).
- **US3 (CLIs)** depends on US2 being the target posture (CLIs must work against auth-on).
- **US4 (BFF)** and **US5 (docs/tests)** are independent and parallelizable.
- Cross-cutting regression (T102) lands last.

### Constitution gates (re-check each phase)
- Principle I strengthened: loopback binding removes LAN exposure (verify in US1).
- Principle II unchanged: 005 touches networking/auth/CLIs only — never the VRAM mutex.
- No new runtime → no amendment.
- 002 keyed path + 004 BFF key-hygiene preserved (the provisioned gateway stays authenticated).

## Implementation Strategy

1. **US1 first** → close LAN exposure; prove the WSL daemons + browser still work. **Stop and validate.**
2. **US2** → fail-closed default with an explicit dev override.
3. **US3 → US4 → US5** → CLIs, BFF robustness, docs/test hygiene.
4. Each phase ends with its test passing on the target machine; never regress 001–004.

## Out of Scope (audit findings not actioned)
- **#7 CSP `unsafe-inline`** — deliberate 004 decision for a localhost single-operator console; revisit
  only if the console is exposed beyond the host (spec.md → Non-Goals).
- **Per-request key hot-reload** — documented as restart-required (FR-046); live reload not pursued.
