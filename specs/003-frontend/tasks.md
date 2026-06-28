---
description: "Task list for MLOps-Lite Operator UI (003)"
---

# Tasks: MLOps-Lite Operator UI

**Input**: Design documents from `specs/003-frontend/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the feature-complete (001) +
hardened (002) platform.

**Tests**: Lightweight per-phase smoke/integration tests (constitution VII), run on the target
machine before the next phase. Task IDs continue the shared space (T062+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** ✅ **COMPLETE & validated on hardware.** All phases (0–6) built and
> verified live: constitution v1.3.0 ratified; gateway SSE (`/infer/stream`, `/platform/events`,
> `/runs/{id}/events`); Next.js operator console (`ui/`, native WSL, 127.0.0.1, terminal/man-page
> design) with all six tabs over the BFF (key server-side); `ui` is the 4th supervised daemon in
> `up_all`/`down_all`. Tests pass: T068 (stream), T071 (smoke + supervision flip), T077 (key-leak +
> localhost bind), plus a no-regression sweep (auth/serving/registry/datasets/bento) green and
> SC-016 confirmed (no `ui` compose service / no C: image).

## Phase 0: Constitution Amendment (GATE)

- [x] T062 Amend `.specify/memory/constitution.md` to **v1.3.0** with these two clauses (verbatim
  wording locked in [plan.md](./plan.md) → Constitution Check):
  - **(a) Node.js runtime** — MAY be used *solely for the operator UI and its BFF*; the platform stays
    Python + Docker Compose for every other service; Node is confined to `ui/`.
  - **(b) Native non-GPU service** — MAY run as a native host process *when justified by disk-frugality
    (Principle III) and bound to localhost*, extending the GPU-only native-host allowance (v1.2.0).
  State explicitly that **Principle II (one model in VRAM) is unchanged**. Bump version + amendment
  note. No general "any web service" loophole. **No build proceeds until this is ratified.**

**Checkpoint**: The governance gate is open; the runtime + native-UI allowances are documented.

---

## Phase 1: Inference Console (US1, P1)

**Goal**: Stream inference + classify images from the browser, with the key kept server-side.

- [x] T063 [US1] Gateway `gateway/app/routers/stream.py`: `GET/POST /infer/stream` → SSE that proxies
  llama-server's native token stream via the supervisor; same `require_api_key` dependency;
  auto-reconnect-friendly + tolerates the cold-load gap; never log the key (FR-026/FR-027). **Additive**
  — leave the existing REST `/infer` intact (001 smoke + non-UI clients). Mount in `main.py`.
- [x] T064 [US1] Scaffold `ui/` — Next.js (App Router, TS) + Tailwind styled to
  [`design-language.md`](./design-language.md) (**JetBrains Mono** self-hosted, ASCII-bracket glyphs,
  flat cream/ink tokens; no shadcn default); app shell with the six-tab nav; `next.config` + `run.sh`
  binding **127.0.0.1:3000**; expose **`/healthz`** (FR-031, what the supervisor polls) (FR-025/FR-030).
- [x] T065 [US1] BFF `ui/app/api/gw/[...path]/route.ts`: proxy REST **and** SSE to the gateway,
  injecting `X-API-Key` from `GATEWAY_API_KEY` server-side; the key is never sent to the client
  (FR-024).
- [x] T066 [US1] Infer tab: streaming chat (consumes `/infer/stream` via the BFF, renders tokens
  incrementally + latency/model line) and a vision drop-zone (`/vision/classify` → top-5); model
  picker from `/models` (FR-023).
- [x] T067 [US1] Supervisor: add a 4th managed daemon **ui** (`bash ui/run.sh`, health
  `http://localhost:3000/healthz`) in `supervisor/supervise.py`; include it in `up_all`/`down_all` +
  `supervisor_{up,down}.sh` (FR-028).
- [x] T068 [US1] Test `tests/test_stream.py`: `/infer/stream` emits incremental SSE tokens with a
  valid key, `401` without; key absent from the stream. **Checkpoint**: stream + classify from the
  browser; key never browser-visible.

---

## Phase 2: Platform Health (US2, P2)

- [x] T069 [US2] Gateway SSE `state` channel (in `stream.py`): periodic daemon/GPU snapshot
  (reuse `platform_health` + `platform_metrics`) as SSE (FR-029). → `GET /platform/events`.
- [x] T070 [US2] Health tab: live tiles (daemon states, GPU free, serving-resident) from
  `/platform/health` + the SSE `state` channel; **embed the existing Grafana dashboard** panels
  (iframe) for history (FR-029). Grafana `allow_embedding` enabled in compose.
- [x] T071 [US2] Test `tests/test_ui_smoke.*`: Health reachable through the UI; kill a daemon → its
  tile flips unhealthy→healthy within the bound (ties to US2 supervision).

**Checkpoint**: at-a-glance live health + embedded history, no chart rebuild.

---

## Phase 3: Models & Datasets (US3, P3)

- [x] T072 [P] [US3] Models tab: list models + versions (`/models`), promote a version
  (`/models/{name}/promote`); the Infer picker reflects the new serving pointer.
- [x] T073 [P] [US3] Datasets tab: upload (base64 → `/datasets`), list versions, show sha256/size;
  surface idempotency (identical bytes → same version).

**Checkpoint**: registry + data manageable from the UI; promotion repoints serving.

---

## Phase 4: Training Runs (US4, P4)

- [x] T074 [US4] Gateway `/runs/{id}/events` SSE: gateway polls the trainer's `GET /train/{id}` and
  re-emits status/metrics as SSE; same auth (FR-026).
- [x] T075 [US4] Runs tab: launch form (dataset version, output name, steps, lora_r, seed) → `/runs`;
  live run view via `/runs/{id}/events`; show the resulting registered, promotable version; surface
  the one-model-in-VRAM refusal clearly (Principle II).

**Checkpoint**: launch + watch a run to a registered version, live.

---

## Phase 5: Monitoring & Drift (US5, P5)

- [x] T076 [US5] Monitor tab: run a drift check (**REST** `/monitor/check`, reference vs current
  dataset version), show the result (per-feature PSI, dataset_drift); if a retrain launches, surface
  it and **link into the live Runs view** — no dedicated monitor SSE (a check returns one report).

**Checkpoint**: drift check + result + retrain visibility that flows into the live Runs view.

---

## Phase 6: Cross-Cutting Security & Regression

- [x] T077 Security test `tests/test_ui_security.py`: the API key never appears in any
  browser-visible payload / bundle / browser-originated request (SC-014); the UI is bound to
  `127.0.0.1` and unreachable on the LAN (SC-015).
- [x] T078 No-regression: the full 001/002 suite still passes with the SSE endpoints + UI present
  (SC-018); `up_all` brings the UI up healthy and adds **no C: image** (SC-016).

---

## Dependencies & Execution Order

- **Phase 0 (amendment)** gates all build phases.
- **US1** establishes the shell + BFF + the first SSE path; **US2** adds the live state channel;
  **US3** is mostly REST (parallelizable); **US4/US5** each add one SSE bridge + its tab.
- Cross-cutting security + regression lands last but its key-leak rule (SC-014) is honored from US1.

### Constitution gates (re-check each phase)
- Principle II unchanged: the UI is read/proxy; it surfaces but never bypasses the VRAM mutex.
- Principle III: the UI stays native WSL (no C: image); verify in US1 + T079.
- Auth parity (FR-027): every SSE endpoint enforces the same key as its REST counterpart.

## Implementation Strategy

1. **Phase 0** → ratify v1.3.0. **Stop and confirm.**
2. **US1** → streaming Infer + vision, key server-side, UI supervised + in one-command flow. Validate.
3. **US2 → US5** → health, registry/data, runs, monitor — each verified on the target machine.
4. Security + regression pass; never regress 001/002.
