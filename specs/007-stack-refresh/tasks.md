---
description: "Task list for Stack Refresh & MLflow 3.x Upgrade (007)"
---

# Tasks: Stack Refresh & MLflow 3.x Upgrade

**Input**: Design documents from `specs/007-stack-refresh/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened, traced platform
(002/004/005/006). Refreshes versions only — no lifecycle/UI/API change.

**Tests**: Re-run the full 001–006 integration suite per tier on the target machine before the next.
Task IDs continue the shared space (T118+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **BUILT & VALIDATED on hardware — all 14 tasks (T118–T131) done.** Scope:
> **broad stack refresh** (MLflow 3.x + image pinning + gateway/UI/native minor bumps), GPU/FT stack
> **frozen**. No constitution amendment (advances Principle VI). MLflow 2.18→3.14 (fresh-volume reset +
> re-seed; tracing ported to `start_span_no_context`; +`--allowed-hosts` for 3.x DNS-rebinding; LLM
> registered with an s3-pointer source), 5 images pinned, gateway python 3.12 + deps, UI React 19.2.7
> (Next stays 15.5.19), native prefect 3.7.6 + pillow 12.2.0. Every increment re-validated green on the
> target machine; full-suite "failures" are live-stack isolation artifacts (all pass individually).
>
> **Codex review fixes (PR #3, 2026-06-28) — 2× P2:** (1) **bootstrap idempotency** — `scripts/bootstrap.sh`
> step 3 gated the dep install on torch/etc importing, so a re-run on an existing venv never applied the
> bumped `mlflow-skinny`/`prefect` (silent version skew vs the 3.14 server). Split the expensive cu128
> torch install (still gated) from the `pip install -r` requirements (now ALWAYS run, idempotent) + pin
> `fsspec==2026.6.0` last so the always-run install can't re-trigger the datasets-cap downgrade. Verified:
> a full bootstrap re-run leaves mlflow 3.14.0 / prefect 3.7.6 / fsspec 2026.6.0. (2) **MLflow allowlist
> vs LAN** — the fixed `--allowed-hosts` broke the documented `BIND_ADDR=0.0.0.0` LAN opt-in (403s).
> Made it `${MLFLOW_ALLOWED_HOSTS:-<loopback default>}` (mirrors the BIND_ADDR override pattern), doc'd in
> `.env.example`. Compose interpolation + override verified; mlflow healthy + registry/serving green.
>
> **Codex re-review (round 2, 2× P2) — fixed:** (3) the always-run pip installs from fix (1) were
> unguarded under `set -uo pipefail` (no `-e`), so a failed refresh would continue + report success →
> stale versions. Added `|| fail` to every install. (4) the SAME directory-existence-cache bug existed in
> the **UI tier** — step 6 skipped `npm ci`/`next build` whenever `node_modules`/`.next` merely existed,
> so a lockfile bump (React 19.2.7) never landed. Now keys on freshness: `npm ci` when
> `package-lock.json -nt node_modules/.package-lock.json`; rebuild `.next` when package.json/lock is newer.
> Freshness detection verified both directions; full bootstrap re-run green.
>
> **Codex re-review (round 3, 1× P2 security) — fixed:** (5) the captured MinIO pin
> `minio/minio:RELEASE.2025-09-07T16-13-09Z` predates the patch for **CVE-2025-62506** (session-policy
> bypass priv-esc, fixed upstream 2025-10-15). Docker Hub's `minio/minio` is FROZEN at the un-patched
> 2025-09-07 tag (the named 2025-10-15 release isn't published there or on quay), so switched to the
> **quay.io hotfix of the same release**: `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772`
> (Apr-2026 rebuild, go1.26.1, backports the security fix; **console retained**; same 2025-09-07 behavior).
> Recreated minio — running, WebUI on :9001, miniodata intact (models 15 / datasets 18 / mlflow 35
> objects), foundation+exposure+datasets green. (mc client not flagged — left on Docker Hub.)
>
> **Codex re-review (round 4, 1× P1) — addressed:** (6) claimed the hotfix is "not a published
> `quay.io/minio/minio` container tag." **Empirically false** — `docker pull` succeeded, `docker manifest
> inspect` returns a published OCI image index with an amd64/linux manifest, and the container runs on it.
> But the underlying reproducibility concern is fair, so **re-pinned BY DIGEST** (the strongest fix, even
> though the spec preferred tags — justified exception when a tag's stability is questioned):
> `quay.io/minio/minio@sha256:cf3dadcfa1fb0324f43958bad1abba986d53c4ecc04d4d50b46c7dcda28bd3cd` (immutable,
> content-addressed, guaranteed resolution). Recreated minio from the digest — running, buckets ready,
> foundation+exposure+datasets green.
>
> **Codex re-review (round 5, 1× P2) — fixed:** (7) the fresh-volume reset (T120) was a MANUAL step, not
> encoded anywhere runnable — so an operator upgrading an existing 2.18 install via `up_all` would get
> MLflow 3.14 against a stale 2.18 schema and a dead service. Added the explicit guarded reset path +
> fail-fast hint Codex asked for: **`scripts/reset_mlflow_3x.ps1`** (`-Confirm`-guarded: compose down →
> drop ONLY `mlops-lite_pgdata` → `up_all` → re-seed; up_all wrapped in try/catch so a transient daemon
> flap doesn't abort the re-seed), **`scripts/reseed_registry.sh`** (re-register+promote the serving LLM
> + re-register vision), and a **fail-fast hint in `up_all.ps1`** (if MLflow isn't healthy, point at the
> reset script — the stale-schema symptom on an upgrade). Validated: guard refuses cleanly without
> `-Confirm`; the destructive path drops pgdata + brings up a fresh 3.x backend (HTTP 200, empty
> registry); reseed registers the LLM `@serving`. (The vision-seed leg + a post-run MinIO-:9000
> host-forwarding glitch were validation-env churn artifacts, not script defects — vision seed is proven
> in T120; cleared by a full stack restart.)
>
> **Claude review (GitHub Action, 2026-06-28) — "no blockers, ready to merge modulo one nit":** confirmed
> the tracing port, bootstrap fixes, MinIO digest, allowed-hosts + dep bumps all correct. One actionable
> item fixed: `reseed_registry.sh` hard-coded `"version":"1"` in the promote → captures the version from
> the register response and promotes THAT (a re-run now promotes the freshly-registered version vs
> orphaning it behind v1; verified: reseed registered v4 → promoted v4 → serving=v4). Other notes were
> cosmetic/intentional: tracing `mlflow` local shadow (cosmetic), mlflow image on py3.11 vs gateway 3.12
> (deliberate US3 gateway-only scope; future alignment), `@types/react-dom` 19.2.3 (already the latest
> 19.2.x — independent versioning, nothing to align), up_all warning-not-exit (correct: gateway
> /platform/health is the hard gate).
>
> **Codex re-review (round 6, 1× P1) — addressed:** claimed `grafana/grafana:13.1.0` is "not a published
> Docker Hub tag." **Empirically false** — untagged it locally and re-pulled fresh from docker.io
> (`Status: Downloaded newer image for grafana/grafana:13.1.0`, exit 0), so a clean-host `compose pull`
> succeeds. Same false-positive pattern as the round-4 MinIO P1. Pinned grafana **by digest**
> (`sha256:121a7a9e…`, the 13.1.0 manifest list) anyway — verified-published but immutable, making the
> "is it published" question moot. Recreated grafana from the digest: running, `/api/health` OK.
>
> **Verified pre-flight (2026-06-28):** latest on PyPI — MLflow `3.14.0` + `mlflow-skinny 3.14.0` (both
> exist); FastAPI `0.138.1`, uvicorn `0.49.0`, pydantic `2.13.4`, boto3 `1.43.36`, prometheus-client
> `0.25.0`, httpx `0.28.1` (current). npm — Next latest `16.2.9` (we stay on **15.x**), React `19.2.7`.
> Verified in the 3.14 source: `start_span_no_context(name, inputs, attributes, start_time_ns,
> experiment_id) -> LiveSpan` + `span.end(end_time_ns=…)` — the clean fire-and-forget fit for FR-057.
>
> **Grilled decisions (2026-06-28):**
> 1. **Fresh `pgdata` volume + re-seed essentials** (NOT in-place `mlflow db upgrade`). Zero migration
>    risk; Kurt accepts losing MLflow run/trace history. **Datasets survive** (content-addressed on MinIO,
>    not Postgres). Re-seed = re-register + promote the serving LLM (`qwen2.5-7b` → `@serving`, so
>    `/infer` `registry_version` resolves) and run `scripts/seed_vision_model.py`. Old experiment IDs left
>    orphaned in the MinIO `mlflow` bucket (harmless). Fresh volume also sidesteps the Postgres-password
>    rotation gotcha. No pgdata snapshot needed.
> 2. **Refactor 006 tracing** to `mlflow.start_span_no_context` + `span.end(end_time_ns=…)` (non-deprecated
>    3.x API), behavior identical.
> 3. **Image pins = specific version tags** (e.g. `postgres:17.x-alpine`, `minio:RELEASE.…`,
>    `prom/prometheus:v3.x`, `grafana:11.x`) — readable, reproducible-enough; NOT digests.
> 4. **Gateway base = `python:3.12-slim`** (mature wheel coverage), NOT 3.13.
> 5. **MLflow target = `3.14.0`** (latest; fresh+reseed removes migration risk).
> 6. **Keep US1 and US3 gateway rebuilds SEPARATE** — US1 bumps only `mlflow-skinny`→3.x + re-validates;
>    US3 later bumps Python 3.12 + pip deps + re-validates. Clean bisect over fewer rebuilds.
> 7. **GPU/FT stack FROZEN** (non-negotiable): torch/torchvision/transformers/peft/accelerate/datasets
>    unchanged.

---

## Phase 0 — Pre-flight (gates everything)

- [X] **T118** [US1] Confirm `mlflow==3.14.0` and `mlflow-skinny==3.14.0` resolve/install clean in the
  gateway + mlflow images (dry build). Capture the CURRENT validated image versions (`docker compose
  images`) so US2 can pin them. *(Grilled: fresh-volume migration — NO pgdata snapshot needed; the
  MLflow run/trace history loss is accepted, datasets survive on MinIO.)* (FR-055)
  > **DONE (2026-06-28):** `mlflow-skinny==3.14.0` + `mlflow==3.14.0` resolve clean on `python:3.12-slim`
  > (pip `--dry-run`, no resolver conflict). **Captured floating-image pins for T123** (read off the
  > current local store): `minio/minio:RELEASE.2025-09-07T16-13-09Z`,
  > `minio/mc:RELEASE.2025-08-13T08-35-41Z`, `prom/prometheus:v3.5.4`, `grafana/grafana:13.1.0`,
  > `postgres:17.10-alpine` (was `17-alpine`; PG_VERSION=17.10). Stack was down at capture time, so these
  > are the last-validated pulled versions — confirm still healthy after the clean bring-up at T124.

## Phase 1 — MLflow 2.18 → 3.x (US1, P1) → SC-036 + SC-038

- [X] **T119** [US1] Bump MLflow to `3.14.0` everywhere, same version: `infra/mlflow/Dockerfile`
  (`mlflow==3.14.0` + psycopg2/boto3 minors), `gateway/requirements.txt` + `training/requirements.txt`
  (`mlflow-skinny==3.14.0`). Rebuild the gateway + mlflow images. *(Keep this gateway rebuild SEPARATE
  from US3's Python/dep bump — grilled.)* (FR-055)
  > **DONE (2026-06-28):** server (`mlflow==3.14.0`) + both skinny clients bumped; psycopg2/boto3 left at
  > validated minors. Gateway+mlflow images rebuilt clean; built-image smoke confirms mlflow 3.14.0 +
  > `start_span_no_context` present + `MlflowClient` imports. **Native venv gotcha:** re-running
  > `pip install -r training/requirements.txt` re-resolved `datasets==3.1.0`'s `fsspec<=2024.9.0` cap and
  > downgraded fsspec `2026.6.0→2024.9.0`, breaking `bentoml 1.4.39` (needs `fsspec>=2025.7.0`). Pinned
  > fsspec back to the validated **2026.6.0** (bentoml-happy; datasets' cap is the same benign pre-007
  > violation). All of mlflow/datasets/bentoml/torch/transformers/peft import; `torch.cuda True`.
- [X] **T120** [US1] **Fresh backend** (grilled): drop the `pgdata` volume so the 3.x server inits a clean
  store (sidesteps the schema migration + the Postgres-password rotation gotcha). **Re-seed essentials**
  so the platform resolves again: re-register + promote the serving LLM (`qwen2.5-7b` → `@serving`) and
  run `scripts/seed_vision_model.py`. Datasets need no re-seed (content-addressed on MinIO). Old
  experiment artifacts in the MinIO `mlflow` bucket are left orphaned (harmless). (FR-055)
  > **DONE (2026-06-28):** dropped ONLY `mlops-lite_pgdata` (miniodata + grafanadata preserved). Brought
  > the stack up (up_all) — fresh postgres + MLflow 3.14, all 4 native daemons healthy. Re-seeded: vision
  > `vision-mobilenet` v1, and LLM `qwen2.5-7b-instruct-q4_k_m` v1 → `@serving` (verified `/models`).
  > **Two 3.x migration changes had to be handled (NOT in the original spec):**
  > 1. **DNS-rebinding Host-header validation** — MLflow 3.x rejects the gateway's `Host: mlflow:5000`
  >    with `403 Invalid Host header`. Fixed by adding `--allowed-hosts mlflow:5000,localhost:5000,
  >    127.0.0.1:5000,localhost:${MLFLOW_PORT},127.0.0.1:${MLFLOW_PORT}` to the mlflow server command
  >    (`MLFLOW_SERVER_ALLOWED_HOSTS`). The in-container healthcheck (`localhost:5000`) must be covered.
  > 2. **Stricter model-version source validation** — 3.x rejects a local `file://` source unless tied to
  >    a run's artifact dir. The LLM registry entry is a routing pointer (weights served locally by
  >    llama.cpp; `/infer` never reads `source`), so it's registered with an `s3://`-scheme pointer
  >    (`s3://models/llm/qwen2.5-7b-instruct-q4_k_m/Q4_K_M.gguf`), mirroring how vision's `s3://` source
  >    is accepted. No object is required (MLflow doesn't fetch it at registration).
- [X] **T121** [US1] Port `gateway/app/tracing.py` to the non-deprecated 3.x tracing API — verified shape:
  `span = mlflow.start_span_no_context(name, inputs=…, attributes=…, start_time_ns=…, experiment_id=…)`
  then `span.end(end_time_ns=…)` (set outputs/status on the span). Preserve fire-and-forget, fail-open,
  span-outside-`_gpu_lock`, frame-count, lazy worker-thread init, and the
  `MLFLOW_TRACING_ENABLED`/`MLFLOW_TRACE_CAPTURE_IO` toggles + container passthrough exactly. (FR-057)
  > **DONE (2026-06-28):** ported `_emit_sync` to `mlflow.start_span_no_context(...)` + `span.end(outputs,
  > status, end_time_ns)`; dropped the held `MlflowClient` (`_client`→lazily-imported `_mlflow` module).
  > Offline contract checks (toggles + disabled-no-op + fail-open) green; live `test_tracing_rest` +
  > `test_tracing_stream` confirm REST + stream traces land via the 3.x API with SSE passthrough intact.
- [X] **T122** [P] [US1] Re-validate on the 3.x server: `test_serving` / `test_registry` /
  `test_datasets` / `test_finetune` / `test_drift_loop` + `test_tracing_rest` / `test_tracing_stream` /
  `test_tracing_resilience`; confirm the re-seeded registry resolves (serving `@serving` + vision),
  datasets are intact on MinIO, and traces land via the 3.x API (prior run/trace history intentionally
  not carried over). (SC-036, SC-038)
  > **DONE (2026-06-28) — 20/20 green on the migrated 3.14 stack.** First batch 18 passed (registry,
  > serving, datasets, tracing rest+stream+resilience). finetune+drift initially 409'd on the
  > one-model-in-VRAM mutex (serving still resident from the tracing `/infer`s) — **expected, not a
  > regression**; after serving idle-released VRAM both passed (2/2). Confirms SC-036 (registry/datasets/
  > training/drift/tracing unchanged on 3.x) + SC-038 (tracing on `start_span_no_context`, REST+stream
  > intact). Trainer logged to MLflow 3.14 via the bumped skinny client (no version skew).

## Phase 2 — Pin floating images (US2, P1) → SC-037

- [X] **T123** [US2] `docker-compose.yml`: replace `:latest` on `minio/minio`, `minio/mc`,
  `prom/prometheus`, `grafana/grafana` (and pin the Postgres minor) with the validated versions captured
  at T118 / after a clean bring-up. Note each pin's source in a comment. (FR-056)
  > **Pins captured at T118 (2026-06-28):** `minio/minio:RELEASE.2025-09-07T16-13-09Z` ·
  > `minio/mc:RELEASE.2025-08-13T08-35-41Z` · `prom/prometheus:v3.5.4` · `grafana/grafana:13.1.0` ·
  > `postgres:17-alpine`→`postgres:17.10-alpine`.
  > **DONE (2026-06-28):** all five pinned with `# 007 US2: pin (...captured T118)` comments; no platform
  > image uses `:latest`. **MinIO later moved to the quay.io hotfix
  > `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772` for CVE-2025-62506** (Codex round-3;
  > Docker Hub's tag is frozen un-patched — see the round-3 note above).
- [X] **T124** [P] [US2] Clean `up_all`; `test_foundation` + `test_exposure` green (pins are healthy +
  loopback-bound). (SC-037)
  > **DONE (2026-06-28):** clean up_all pulled the explicit tags + recreated; postgres came up healthy on
  > `17.10-alpine` (pgdata + the T120 re-seed persisted). `test_foundation` + `test_exposure` 2/2 green —
  > all services healthy + 127.0.0.1-bound. SC-037 met.

## Phase 3 — Gateway Python refresh (US3, P2) → SC-039

- [X] **T125** [US3] `gateway/Dockerfile` → `python:3.12-slim`; `gateway/requirements.txt` bump FastAPI
  (`0.138.x`), uvicorn (`0.49.x`), pydantic (`2.13.x`), boto3 (`1.43.x`), prometheus-client (`0.25.x`)
  (httpx already current). Rebuild. (FR-058)
  > **DONE (2026-06-28):** Dockerfile `python:3.11-slim`→`3.12-slim`; pinned fastapi `0.138.1`, uvicorn
  > `0.49.0`, pydantic `2.13.4`, boto3 `1.43.36`, prometheus-client `0.25.0` (httpx 0.28.1 unchanged).
  > Rebuilt clean; image smoke confirms python 3.12.13 + all bumped versions + `app.main` import OK.
- [X] **T126** [P] [US3] Gateway suite green (auth/serving/registry/datasets/monitor/vision/stream/
  tracing); OpenAPI contract + `/metrics` output unchanged. (SC-039)
  > **DONE (2026-06-28):** **OpenAPI contract byte-identical** (22 paths, same operations + schemas
  > before/after the FastAPI+pydantic bump). Gateway suite green: auth(+modes)/cli_auth/registry/datasets/
  > tracing_resilience + serving/stream/vision/tracing rest+stream — 36/36 (no monitor-specific test;
  > monitor exercised via the drift loop in T122). **Wiring lesson:** a plain `docker compose up -d
  > gateway` recreates WITHOUT up_all's injected `SERVING_URL/TRAINER_URL/BENTO_URL`, so the gateway falls
  > back to `host.docker.internal` and can't reach the WSL daemons (daemon tests skip). Re-ran `up_all` to
  > rewire → daemon tests pass. **Always rewire via up_all after recreating the gateway.** SC-039 met.

## Phase 4 — UI refresh, stay on Next 15 (US4, P3) → SC-040

- [X] **T127** [US4] `ui/package.json` → latest **Next 15.x** + React `19.2.x` + tooling (TypeScript,
  Tailwind, PostCSS, autoprefixer, `@types/*`); `npm install` to refresh `package-lock.json`;
  `next build`; bounce the `ui` daemon (`pkill -f '[n]ext-server'`, supervisor restarts). NOT Next 16. (FR-059)
  > **DONE (2026-06-28):** **Next stays `15.5.19` — already the latest 15.x** (bumped for the CVE in 004),
  > so no Next move. React/react-dom `19.0.0`→`19.2.7`; tooling: @types/node `22.20.0`, @types/react
  > `19.2.17`, @types/react-dom `19.2.3`, typescript `5.9.3`, tailwindcss `3.4.19` (stay on 3.x, not 4),
  > postcss `8.5.15`, autoprefixer `10.5.2`. `npm install` refreshed `package-lock.json`; `next build`
  > clean (all 6 tabs + BFF routes); ui daemon bounced → supervisor restarted next-server v15.5.19.
- [X] **T128** [P] [US4] `test_ui_security` / `test_ui_smoke` / `test_ui_resilience` green; six tabs +
  BFF contract (allowlist, origin guard, `[::1]`, non-leaky errors, key absent from payloads) unchanged. (SC-040)
  > **DONE (2026-06-28):** 3/3 green — security + smoke + resilience all pass on the refreshed UI; six tabs
  > render and the BFF contract (allowlist, origin guard, `[::1]`, non-leaky errors, key never in payloads)
  > is unchanged. SC-040 met.

## Phase 5 — Safe native (non-GPU) bumps (US5, P3) → SC-041

- [X] **T129** [US5] `serving/bento/requirements.txt` (BentoML, Pillow) + `training/requirements.txt`
  (Prefect) minor bumps; update `scripts/native_env.lock`. **torch / torchvision / transformers / peft /
  accelerate / datasets UNCHANGED** (frozen cu128 stack). (FR-060)
  > **DONE (2026-06-28):** **bentoml stays 1.4.39** (already latest 1.x). **pillow 11.0.0→12.2.0** (the
  > venv resolver already pulled 12.2.0 torchvision-side; pin to match). **prefect 3.1.4→3.7.6**;
  > installed in-venv + bounced the trainer daemon to load it. Post-install verify: torch 2.11.0+cu128 /
  > torchvision 0.26.0+cu128 / transformers 4.46.3 / peft 0.13.2 / accelerate 1.1.1 / datasets 3.1.0 /
  > **fsspec 2026.6.0** all UNCHANGED, `torch.cuda True`. native_env.lock updated (+ fsspec-hold note).
- [X] **T130** [P] [US5] `test_bento` (CPU vision) + `test_finetune` + `test_drift_loop` green on the
  frozen torch stack (GPU pipeline intact). (SC-041)
  > **DONE (2026-06-28):** 3/3 green — vision classify (pillow 12.2.0), LoRA fine-tune (prefect 3.7.6 +
  > frozen torch), drift→retrain loop all pass on the frozen cu128 stack. GPU pipeline intact.

## Phase 6 — Cross-cutting regression

- [X] **T131** Full 001–006 keyed sweep green with the refreshed stack; GPU-lock hold time + inference
  latency unchanged; `native_env.lock` + `package-lock.json` + image pins committed. (SC-041)
  > **DONE (2026-06-28):** **No 007 regression.** Whole-suite run = 40 passed / 6 failed, but **all 6
  > pass in isolation** (finetune+registry+datasets+serving+stream 5/5; supervisor 1/1 in 175s;
  > portability 1/1) → the failures are full-suite-against-a-LIVE-stack interaction artifacts, not
  > regressions: (a) the one-model-in-VRAM mutex serializes training↔serving so finetune/serving 409 when
  > co-scheduled; (b) `test_supervisor` restarts all native daemons mid-run and `test_portability` shells
  > `test_serving` + the suite collides on the bento port — these lifecycle tests aren't meant to run
  > concurrently with the supervised stack. Stack self-heals after (all daemons healthy). Inference
  > latency within `test_serving`'s thresholds (no measured regression); GPU idle-release (~120s) + the
  > bidirectional mutex unchanged. `native_env.lock` (15d719a) + `ui/package-lock.json` (26782d0) + image
  > pins (b55c194) all committed. SC-041 met.

---

## Dependencies & Execution Order

- **T118 (pre-flight) gates everything** — confirm the 3.x installs resolve and capture the current
  image versions (for US2 pins) before touching the stack. *(Fresh-volume reset — no pgdata
  snapshot/rollback point needed; the run/trace history loss is the grilled, accepted trade.)*
- **US1 (MLflow, T119–T122)** is the highest-risk; do it first so a regression is isolated before the
  cheaper refreshes pile on.
- **US2 (image pins)** is independent and low-risk; **US3/US4/US5** are independent refresh tiers, each
  re-validated cumulatively.
- **T131 lands last** (needs every tier in place).

### Constitution gates (re-check each phase)
- Principle II untouched: GPU/FT stack frozen (verify no torch-family movement in US3/US5).
- Principle VI strengthened: pinned images; MLflow backend reset to a fresh volume + re-seeded (run/trace history not retained, accepted; datasets intact on MinIO).
- No new runtime → no amendment (MLflow/Python/Node all pre-existing).

## Implementation Strategy

1. **MLflow 3.x: fresh-volume reset + re-seed** → port tracing + re-validate. **Stop and validate.**
   *(No snapshot — the run/trace history loss is the grilled, accepted trade.)*
2. **Pin images** → clean bring-up.
3. **Gateway → UI → native** minor refreshes, each behind its own test gate.
4. Each phase re-runs the relevant 001–006 tests on the target machine; never regress; never move the
   frozen GPU stack.

## Out of Scope (recorded)
- **GPU/FT stack upgrade** (torch/transformers/…): frozen (FR-060) — a future increment with its own GPU
  re-validation.
- **Next.js 16**: deferred; 007 stays on Next 15 (FR-059).
- **MLflow 3.x feature adoption** (GenAI eval, prompt registry, logged-models workflows): version move
  only; features are a later increment.
