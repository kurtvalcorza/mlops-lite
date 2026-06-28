# Feature Specification: MLOps-Lite Operator UI

**Feature Branch**: `003-frontend`

**Created**: 2026-06-28

**Status**: Draft

**Input**: User description: "A localhost operator control plane (web UI) over the feature-complete,
hardened platform: run inference (streaming), classify images, browse/promote models, manage
datasets, launch and watch training runs, run drift checks and see retrains, and see live
daemon/GPU health — all through the gateway, with the API key kept server-side."

> **Scope note**: 003 adds an **operator UI** (a new surface) plus the **SSE endpoints** on the
> gateway that feed it. It adds **no lifecycle stage** (US1–US5 of 001 are unchanged) and changes
> no daemon behavior. It is the first increment to introduce a **frontend + a Node.js runtime** and
> to run a **non-GPU service natively** on the WSL host — both of which require a constitution
> amendment (see plan.md → Constitution Check). Requirement IDs continue the shared space (FR-023+,
> SC-013+); tasks continue T062+.

> **Definition of Done**: the increment is complete only when **all six tabs** ship (US1–US5). The
> prioritized stories are the **build order** — each independently runnable and verifiable on the
> target machine (Principle VII) — not a partial release; "done" = the full control plane.

## Non-Goals *(scope guard)*

Explicitly **out of scope** for 003 (consistent with the single-operator / localhost / key-in-BFF
model) — adding any of these later requires its own increment:

- **No multi-user / accounts / RBAC** — single local operator; no UI login (see Assumptions).
- **No in-UI secret or settings editing** — credentials stay in `.env` / `gen_secrets`; the UI never
  reads or writes them.
- **No model-zoo / download manager** — model seeding stays in `bootstrap.sh` / the registry API.
- **No raw log-file streaming / terminal** — run progress comes from `/runs/{id}/events`, not log tails.
- **No remote/LAN exposure or TLS** — bound to `127.0.0.1`; revisit only if ever exposed beyond the host.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Inference console (Priority: P1)

The operator opens a browser to the platform and, without touching `curl`, sends a prompt and
watches the model's response **stream token-by-token**, and drops an image to get a top-5
classification. A model picker reflects what's registered/promoted.

**Why this priority**: Inference is the platform's MVP (001 US1) and the most interactive surface —
it delivers immediate, visible value and exercises the streaming path end-to-end.

**Independent Test**: From the UI, a prompt streams a completion (tokens appear incrementally, not
all-at-once) and reports latency; an uploaded image returns five labeled scores. The browser never
sees the API key.

**Acceptance Scenarios**:

1. **Given** the platform is up, **When** the operator submits a prompt, **Then** tokens render
   incrementally via SSE and a final latency/model line is shown.
2. **Given** an image, **When** the operator submits it, **Then** the UI shows the top-5 labels and
   scores from the vision service.
3. **Given** any inference call, **When** the browser's network traffic is inspected, **Then** no
   `X-API-Key` / credential appears (only the server-side BFF→gateway hop carries it).

---

### User Story 2 - Platform health & observability (Priority: P2)

The operator sees, at a glance, whether each native daemon (serving, training, vision) is healthy,
how much GPU VRAM is free, and whether a model is resident — updating live — with the existing
Grafana dashboard embedded for history.

**Why this priority**: Read-only, low-risk, and it validates the app shell + the live (SSE) state
channel before any mutating tab is built. High operational value for a self-hosted platform.

**Independent Test**: With the platform up, the health tiles show all daemons healthy and the GPU
free figure; killing a daemon flips its tile to unhealthy within the bound (supervisor restarts it);
the embedded Grafana panels render.

**Acceptance Scenarios**:

1. **Given** the platform is up, **When** the operator opens Health, **Then** live tiles show each
   daemon's state + GPU free + serving-resident, sourced from `/platform/health` and an SSE stream.
2. **Given** a daemon dies, **When** the operator is on Health, **Then** its tile reflects the
   transient unhealthy→healthy transition without a manual refresh.
3. **Given** the Health view, **When** it loads, **Then** the existing Grafana dashboard panels are
   embedded for historical charts (no chart rebuild).

---

### User Story 3 - Model registry & datasets (Priority: P3)

The operator browses registered models and their versions, promotes a version to `serving`, and
uploads / browses immutable dataset versions.

**Independent Test**: The UI lists models and versions; promoting a version repoints `serving` and
the Infer model picker reflects it; uploading a dataset shows a new content-addressed version;
re-uploading identical bytes is idempotent (same version).

**Acceptance Scenarios**:

1. **Given** registered models, **When** the operator promotes a version, **Then** `/models/{name}`
   serving pointer moves and the change is visible in the UI and the Infer picker.
2. **Given** a CSV/JSONL file, **When** the operator uploads it, **Then** a new dataset version
   appears with its sha256/size; identical content does not create a duplicate.

---

### User Story 4 - Training runs (Priority: P4)

The operator launches a LoRA fine-tune against a pinned dataset version and watches the run progress
live, ending at a registered, promotable model version. The launch form exposes exactly the
trainer's `/runs` fields — **dataset version, output name, steps, lora_r, seed** — with any advanced
knobs defaulted/hidden (the form stays honest to the backend).

**Independent Test**: Launching a run returns a run id; the run view updates live (SSE) through to
`completed`; the resulting registered version is shown, lineage-linked, and promotable.

**Acceptance Scenarios**:

1. **Given** a dataset version, **When** the operator launches a run, **Then** the run view streams
   status updates (via `/runs/{id}/events`) without manual refresh.
2. **Given** a completed run, **When** it finishes, **Then** the new model version is shown with its
   dataset/base lineage and a promote action.
3. **Given** a model is resident in serving, **When** a run is launched, **Then** the UI surfaces the
   one-model-in-VRAM refusal clearly (Principle II is enforced by the backend, surfaced by the UI).

---

### User Story 5 - Monitoring & drift (Priority: P5)

The operator runs a drift check (reference vs current dataset), watches it live, and sees whether a
retraining run was triggered.

**Independent Test**: A stable check shows no drift/no retrain; a shifted check flags drift (PSI ≥
threshold) and, if the trainer is up, surfaces the launched retrain run, followed live in Runs.

**Acceptance Scenarios**:

1. **Given** two dataset versions, **When** the operator runs a drift check, **Then** the result
   (per-feature PSI, dataset_drift flag) is shown (request/response — a check returns one report).
2. **Given** a breach with a retrain spec, **When** drift is detected, **Then** the launched retrain
   run is surfaced and links into the **live Runs view** (the run streams there, not here).

---

### Edge Cases

- **Key never in the browser**: every gateway call is proxied by the BFF; the key lives only
  server-side. A leaked key in any browser-visible payload is a defect (SC-014).
- **UI is an unauthenticated front door**: mitigated by binding to `127.0.0.1` only (FR-025); the UI
  must not be reachable from the LAN.
- **Daemon down**: tabs that need a daemon (Infer, Runs) show a clear "daemon unreachable" state, not
  a stack trace; Health reflects it.
- **One-model-in-VRAM contention**: launching training while serving is resident (or vice-versa) is
  refused by the backend; the UI surfaces the 409/refusal plainly.
- **SSE disconnect / cold-load**: a dropped stream auto-reconnects with backoff; the first
  `/infer/stream` token may lag seconds during a serving cold-load — the view shows "loading", not an
  error (FR-027).
- **No C: image**: the UI runs natively in WSL; the build must not add a container image to the tight
  C: drive (FR-030 / Principle III).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-023**: A localhost operator UI MUST present all six surfaces — Infer (chat + vision), Models,
  Datasets, Runs, Monitor, Health — driven entirely through the gateway API.
- **FR-024**: The UI MUST NOT expose the gateway API key to the browser. A server-side BFF (Next.js
  route handlers) holds `GATEWAY_API_KEY` and injects `X-API-Key` on every gateway call.
- **FR-025**: The UI MUST bind to `127.0.0.1` only (no LAN/remote exposure), consistent with the
  single-operator/localhost model (002 assumptions; no UI login).
- **FR-026**: The gateway MUST provide SSE streams **where streaming adds value** — `/infer/stream`
  (token stream proxied from llama-server) and `/runs/{id}/events` (run progress) — by bridging the
  daemons' existing poll APIs; the native daemons remain ~unchanged. Drift checks and vision classify
  stay **request/response** (a drift check returns one report; a retrain it triggers is followed via
  the Runs stream). `/infer/stream` is **additive** — the existing REST `/infer` remains for non-UI
  clients and the 001 smoke tests (no replacement).
- **FR-027**: SSE endpoints MUST enforce the same API key as their non-streaming counterparts (auth
  parity); keys MUST NOT appear in stream payloads or logs. Client SSE consumption MUST **auto-reconnect
  with backoff** and tolerate the serving **cold-load gap** (the first `/infer/stream` token may lag
  seconds while llama-server loads from idle-released VRAM) without erroring the view.
- **FR-028**: The UI MUST run as a native WSL process managed by the US2 supervisor and be brought
  up/down by `up_all` / `down_all` (one-command flow extends to the UI).
- **FR-029**: Health views MUST show live daemon/GPU state (from `/platform/health` + an SSE state
  channel) AND embed the existing Grafana dashboard for historical charts.
- **FR-030**: The UI MUST NOT add a container image to the disk-constrained C: drive (Principle III);
  it runs as native Node on the WSL host (ample disk).
- **FR-031**: The UI MUST expose a `/healthz` endpoint (Principle VI — every service exposes health);
  it is what the US2 supervisor polls to manage the UI daemon.

### Key Entities *(include if feature involves data)*

- **BFF proxy**: the server-side layer that holds the API key and forwards browser requests to the
  gateway (REST + SSE); the only key holder.
- **SSE event**: a typed server-sent event — `token` (inference), `run` (status/metrics), `state`
  (daemon/GPU) — emitted by the gateway, relayed by the BFF. (Drift is request/response, not SSE.)

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-013**: From the UI alone, an operator can: stream an inference, classify an image, browse +
  promote a model, upload + browse a dataset, launch + watch a run to a registered version, and run a
  drift check seeing any retrain trigger — all without `curl`.
- **SC-014**: The API key never appears in any browser-visible payload, JS bundle, or
  browser-originated network request (only the BFF→gateway hop carries it).
- **SC-015**: The UI is unreachable from another host on the LAN (bound to `127.0.0.1`).
- **SC-016**: `up_all` brings the UI up healthy alongside the daemons; `down_all` stops it; no
  container image is added to the C: drive.
- **SC-017**: Streaming inference renders tokens incrementally and the Runs view updates live (SSE)
  without manual refresh; a drift check shows its result and links any triggered retrain into the
  live Runs view.
- **SC-018**: All 001/002 integration tests still pass (no regression) with the SSE endpoints and the
  UI present.

## Assumptions

- **Single local operator, still** — no UI login, no multi-user/RBAC; the UI is bound to localhost
  and the BFF is the sole key holder. (If the UI is ever exposed beyond the host, revisit auth + TLS.)
- The hybrid model stands: GPU/vision daemons stay native; the UI is **also** a native WSL process
  (a non-GPU native service — the change requiring an amendment), chosen for disk-frugality.
- SSE is bridged by the gateway from the daemons' existing poll APIs; the daemons are not rewritten.
- Visual system is the **terminal/man-page design language** in
  [`design-language.md`](./design-language.md) (Tailwind + JetBrains Mono, ASCII-bracket glyphs, flat
  cream/ink; status uses the semantic ramp). Specific layouts/copy are implementation detail.
- This increment changes no lifecycle behavior; it adds a UI surface and the SSE plumbing for it.
