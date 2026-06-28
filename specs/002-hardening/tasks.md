---
description: "Task list for MLOps-Lite Hardening (002)"
---

# Tasks: MLOps-Lite Hardening

**Input**: Design documents from `specs/002-hardening/`

**Prerequisites**: plan.md (required), spec.md (required for user stories); builds on the
feature-complete 001 platform.

**Tests**: Lightweight per-phase **smoke/integration** tests (per the constitution, each phase
runs end-to-end on the target machine before the next). Task IDs continue 001's space (T044+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **002 COMPLETE** — Phases 1 (US1), 2 (US2), 3 (US3) and 4 (US4) all DONE
> & validated on the target machine. 002 hardened 001 operationally; it changed no US1–US5 behavior.
> Constitution gate-check: PASS, no amendment required (see plan.md).

## Phase 1: Authentication & Secret Hygiene (US1, P1) — ✅ DONE (2026-06-28)

**Goal**: Close open access and stop shipping usable secrets, without changing lifecycle behavior.

- [x] T044 [US1] `gateway/app/auth.py`: FastAPI API-key dependency — reads allowed key(s) from a
  local secret (env `GATEWAY_API_KEYS` / `GATEWAY_API_KEYS_FILE`), compares **constant-time**
  (`hmac.compare_digest` over SHA-256 hashes, no early exit), never logs keys; reusable
  `require_api_key` dependency. No keys configured ⇒ runs OPEN with a loud warning (FR-016/FR-022)
- [x] T045 [US1] Applied `require_api_key` to the protected routers in `gateway/app/main.py`
  (infer, models, datasets, runs, monitor, vision) via `include_router(dependencies=…)`;
  `/healthz`, `/metrics`, `/` left open for probes. Gateway → `v1.2.0` (FR-016)
- [x] T046 [P] [US1] Secret hygiene: `docker-compose.yml` MinIO/Postgres/Grafana creds now use
  `${VAR:?…}` (no inline defaults — bring-up **fails fast**); `scripts/gen_secrets.ps1`/`.sh`
  generate random creds + an `mll_` API key into a git-ignored `.env`; `.env.example` documents
  (placeholders only). **Also removed the hardcoded `minioadmin` fallbacks** from the data-plane
  code the guard flagged (gateway/datasets, seed/bootstrap scripts, bento + training daemons) —
  creds now come from the env, no default (FR-017/SC-009)
- [x] T047 [US1] Integration test `tests/test_auth.py`: protected `/models` → `401` with no key
  and with a wrong key, `200` with a valid key; `/healthz` + `/metrics` reachable
  unauthenticated. **PASS** on the live stack (SC-008)
- [x] T048 [US1] No-regression: `tests/_auth.py` threads `GATEWAY_API_KEY` (env) into every 001
  test as `X-API-Key`. Verified live with auth on: foundation, offline, datasets, registry all
  **PASS** with the key; data plane survived credential rotation (SC-012)
- [x] T061 (cross-cutting) `scripts/check_secrets.sh` + `.gitignore` (`.env`/`secrets/`): guard
  greps the tree for committed credential patterns. **PASS** (clean) after the `minioadmin`
  removals above — the guard caught the gap during Phase 1 validation

**Checkpoint**: ✅ Gateway closed to unauthenticated access; no committed production secret; 001 green with a key.

---

## Phase 2: Native-Daemon Supervision (US2, P2) — ✅ DONE (2026-06-28)

**Goal**: Start, health-check, and auto-restart the native WSL daemons — no manual `bash run.sh`.

- [x] T049 [US2] `supervisor/supervise.py` (pure stdlib): declares the managed daemons (serving
  :8090, training :8091, vision :8092) with their run.sh commands + health URLs; starts each, polls
  health, **restarts-on-exit with exponential backoff** (base 1s, cap 30s); after `MAX_RESTARTS`
  consecutive failures marks **persistent-unhealthy** (no crash-loop hammering). SIGTERM/SIGINT
  terminate all children (no GPU orphans). Distinct from `serving/llama/supervisor.py` (model VRAM)
  — this supervises the daemon *processes* and leaves the Principle II mutex untouched (FR-018)
- [x] T050 [US2] Supervisor surfaces state over HTTP `GET :8099/status` — per-daemon name, state
  (pending/starting/healthy/unhealthy/backoff/persistent-unhealthy), restart_count, pid (FR-018)
- [x] T051 [P] [US2] Gateway `GET /platform/health` (`gateway/app/platform_health.py`) aggregates
  serving/training/vision reachability via the daemon URLs it already proxies — open like `/healthz`.
  Gateway → `v1.2.0` (FR-015)
- [x] T052 [US2] Integration test `tests/test_supervisor.py` (WSL): start supervisor → all three
  healthy; kill one → restarted (new pid, restart_count++) to healthy within the bound, others
  untouched. **PASS live on all three real daemons** (SC-010)

**Checkpoint**: ✅ A killed daemon comes back on its own; the platform survives crashes unattended.

---

## Phase 3: One-Command Bring-Up / Teardown (US3, P3) — ✅ DONE (2026-06-28)

**Goal**: Bring the whole platform (Compose infra + native daemons) up and down with one command.

- [x] T053 [US3] `scripts/up_all.ps1` (+ WSL helper `scripts/supervisor_up.sh`): requires `.env`
  (fail-fast) → resolves the dynamic Ubuntu IP and wires `SERVING_URL`/`TRAINER_URL`/`BENTO_URL` →
  `docker compose up -d --build` → starts the supervisor (idempotent — reuses one already on :8099)
  and waits for daemon health → waits until the **gateway** resolves every daemon via
  `/platform/health` (FR-019)
- [x] T054 [US3] `scripts/down_all.ps1` (+ WSL helper `scripts/supervisor_down.sh`): SIGTERMs the
  supervisor (graceful child shutdown), sweeps daemon stragglers, then **authoritatively releases
  the GPU by killing any remaining nvidia-smi compute-app PID** (robust to WSL's `[Not Found]`
  naming — catches `llama-server`/`llama-cli`), verifies VRAM released → `docker compose down` (FR-019)
- [x] T055 [P] [US3] `Makefile` targets `up-all` / `down-all` (via `pwsh -File`); README updated to
  the one-command flow (supersedes the manual `serve_up.ps1` + per-daemon `run.sh` steps)
- [x] T056 [US3] Smoke `tests/test_bringup.ps1` (scripted — orchestrates Win+WSL): up → infra + all
  daemons healthy + gateway resolves all; `down_all` → gateway gone + `nvidia-smi` shows no leftover
  GPU procs; `up_all` restore. **PASS live** (it even cleared a stray Phase-6 `llama-cli`) (SC-010)

**Checkpoint**: ✅ One command to a ready platform, one to a clean stop with the GPU released.

---

## Phase 4: Bootstrap & Portability (US4, P4) — ✅ DONE (2026-06-28)

**Goal**: A clean machine reaches a passing smoke test by editing only the hardware profile.

- [x] T057 [US4] `scripts/bootstrap.sh` (idempotent): parses `VRAM_GB` from hardware-profile.md →
  **gate zero** (native `nvidia-smi`) → creates the `~/mlops-train` venv if missing (venv →
  virtualenv → pip-bootstrap fallback, no sudo) → installs pinned deps (torch cu128 + training +
  bento), verifies `torch.cuda` → **verifies** the `llama-server` CUDA build (prints the recipe if
  missing) → seeds the LLM gguf (local) + vision model (best-effort to MinIO, idempotent guard).
  Re-run = no-op, reports already-ready. **Validated**: idempotent re-run all `[ok]` (FR-020)
- [x] T058 [P] [US4] `scripts/native_env.lock` records the exact GPU-specific wheels
  (`torch==2.11.0+cu128`, `torchvision==0.26.0+cu128`) that requirements.txt leaves to the CUDA
  index; bootstrap installs these pins (FR-021/Principle VI)
- [x] T059 [US4] `tests/test_portability.py` (manual-tier): asserts the retarget contract (profile +
  bootstrap + lock exist, `VRAM_GB` parseable), scans for machine-token leakage outside the profile,
  and runs the serving smoke as the end-to-end proof when serving is reachable. **PASS live**
  (`/infer → pong`; no leakage) (SC-011)
- [x] T060 [P] [US4] README "Setup on a new machine" — one-time prereqs (driver + the manual CUDA
  `llama.cpp` build) vs. what bootstrap automates; cross-links quickstart + native_env.lock

**Checkpoint**: ✅ The genericized template is provably portable — one profile file + bootstrap + smoke.

---

## Phase 5: Cross-Cutting

- [x] T061 Secret-leak guard: `.gitignore` covers `.env`/`secrets/`; `scripts/check_secrets.sh`
  greps the tree for committed credential patterns and fails if found (supports SC-009).
  **DONE alongside US1** — landed in Phase 1 (it found and forced the `minioadmin`-default fix)

---

## Dependencies & Execution Order

- **US1 (auth/secrets)** is the highest-value, lowest-surface hardening — do first; it also makes
  every later phase operate on a secured gateway.
- **US2 (supervision)** depends only on 001's daemons; **US3 (one-command)** wraps US2 + Compose;
  **US4 (bootstrap/portability)** depends on US1–US3 to bootstrap a *hardened* setup.
- Cross-cutting (T061) lands alongside US1 (secret hygiene).

### Constitution gates (re-check each phase)
- Principle II unchanged: the supervisor only starts/restarts daemons — it MUST NOT alter the
  serving↔training one-model-in-VRAM mutex (re-verify after US2/US3).
- Local-First/Lightweight: no new always-on service beyond the tiny stdlib supervisor; no cloud/IdP.
- Gate Zero (FR-020) verified in bootstrap before any serving/training on a new machine.

## Implementation Strategy

1. **US1 first** → secure + secret-clean, 001 still green with a key. **Stop and validate.**
2. **US2 → US3** → unattended, one-command operation.
3. **US4** → prove portability on a clean environment.
4. Each phase ends with its smoke/integration test passing on the target machine; never regress US1–US5.
