# Implementation Plan: MLOps-Lite Operator UI

**Branch**: `003-frontend` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/003-frontend/spec.md`

## Summary

Add an operator **control-plane UI** (Next.js, App Router) over the hardened platform, plus the
**SSE endpoints** on the gateway that feed it. The UI presents all six surfaces (Infer, Models,
Datasets, Runs, Monitor, Health), keeps the gateway API key **server-side in a BFF**, binds to
`127.0.0.1`, and runs **natively in WSL under the US2 supervisor** (no C: image). Streaming is
**gateway-bridged** (the gateway re-emits the daemons' poll APIs as SSE; daemons unchanged).
Delivered in phase-gated slices behind a constitution amendment.

## Technical Context

**Language/Version**: TypeScript on **Node.js 20 LTS** (Next.js 15, App Router) for the UI + BFF;
Python 3.11 (FastAPI) for the new gateway SSE endpoints.

**Primary Dependencies**: Next.js + React, Tailwind CSS styled to the **terminal/man-page design
language** ([design-language.md](./design-language.md)) — **JetBrains Mono** (OSS, self-hosted),
ASCII-bracket glyphs, no shadcn default (small custom flat components; headless Radix only where real
behavior is needed); `eventsource`-style SSE on the client; gateway uses FastAPI `StreamingResponse`
(+ `httpx` streaming to llama-server) — no new heavy gateway dependency. No database for the UI
(stateless BFF).

**Storage**: none new. The BFF is stateless; the key comes from the environment (`GATEWAY_API_KEYS`
/ `GATEWAY_API_KEY`), same local secret source as 002.

**Target Platform**: Win11 + WSL2. The UI is a **native WSL Node process** (like the GPU/vision
daemons), supervised by `supervisor/supervise.py` and wired into `up_all`/`down_all`. The BFF
reaches the gateway at `localhost:8080` (the same bidirectional WSL localhost-forwarding the daemons
use for MLflow/MinIO) — no IP injection needed for UI→gateway.

**Project Type**: full-stack increment — a new `ui/` frontend + BFF, plus gateway SSE additions.

**Performance Goals**: streaming first-token latency bounded by llama-server cold/warm load; SSE
relay adds negligible overhead; Health tiles update at a small interval (~2 s) via SSE.

**Constraints**: keep the key out of the browser (BFF only); bind localhost only; **add no container
image to the tight C: drive** (Principle III) — hence native WSL Node (WSL disk is ample); do not
alter the one-model-in-VRAM mutex (Principle II) — the UI is read/proxy and surfaces the backend's
refusals.

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | UI is localhost-only, offline after pulls; **but** "all services as containers under one Compose stack" — the UI is a *native* non-GPU service | ⚠️ needs amendment (extends the native-host allowance) |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | UI is read/proxy; streaming holds the one resident model during a stream; surfaces (not bypasses) the VRAM mutex | ✅ unchanged |
| III. Lightweight Footprint | Native WSL Node avoids a C: image (the disk reason for the native choice); idle Node RAM is modest | ✅ (and is *why* native was chosen) |
| IV. Full Lifecycle Coverage | No stage added or dropped — the UI is a surface over existing stages | ✅ N/A |
| V. OSS & Swappable | Next.js + Tailwind + JetBrains Mono are OSS (no paid Berkeley Mono); UI sits behind the gateway, swappable | ✅ |
| VI. Reproducibility & Observability | The UI exposes its own health; adds no untracked artifacts; embeds existing Grafana | ✅ |
| VII. Phase-Gated Delivery | Five independently-runnable stories (US1–US5), each verifiable on the target machine | ✅ |
| Workflow: "No other runtime is introduced without amendment" | **Node.js is a new runtime** (alongside Python/Compose) | ⚠️ needs amendment |
| Workflow: native-host pattern | Currently scoped to **GPU-bound** services; the UI is a non-GPU native service | ⚠️ needs amendment |

**Amendment required → constitution v1.3.0.** Two narrow allowances, both well-justified. The exact
clauses to ratify (Phase 0 / T062):

> **Node.js runtime (Amendment, 2026-06-28):** A Node.js runtime MAY be used **solely for the
> operator UI and its BFF**. The platform remains Python + Docker Compose for every other service;
> Node is confined to `ui/`.
>
> **Native non-GPU service (Amendment, 2026-06-28):** A non-GPU service MAY run as a native host
> process **when justified by disk-frugality (Principle III) and bound to localhost** — extending the
> GPU-only native-host allowance (v1.2.0). The operator UI runs natively in WSL on this basis.
> **Principle II (one model in VRAM) is unchanged.**

No general "any web service" loophole — both clauses are scoped to the operator UI.

The alternative that would avoid allowance (2) — running the UI as a **Compose service** — still
needs allowance (1) (Node runtime) AND adds a container image to the scarce C: drive (a Principle III
cost). The native-WSL choice is the more constitution-aligned option *given an amendment for the Node
runtime is unavoidable either way*. The amendment is the first task (Phase 0), gating the build.

## Project Structure

### Documentation (this feature)

```text
specs/003-frontend/
├── plan.md              # This file
├── spec.md              # Feature specification
├── tasks.md             # Task list
└── design-language.md   # Terminal/man-page visual system (adapted from OpenCode)
```

### Source Code (delta over 002)

```text
mlops-lite/
├── ui/                         # NEW: Next.js (App Router) operator console + BFF
│   ├── app/                    #   six routes: infer, models, datasets, runs, monitor, health
│   ├── app/api/gw/             #   BFF route handlers — inject X-API-Key, proxy REST + SSE
│   ├── lib/                    #   gateway client, SSE helpers, types
│   └── run.sh                  #   native WSL launcher (next start, bind 127.0.0.1:3000)
├── gateway/app/
│   ├── routers/stream.py       # NEW: SSE — /infer/stream, /runs/{id}/events (Monitor/vision are REST)
│   └── main.py                 # MODIFIED: mount the SSE router (same auth dependency)
├── supervisor/supervise.py     # MODIFIED: manage a 4th daemon — ui :3000 (127.0.0.1)
├── scripts/{up_all,down_all}.ps1 + supervisor_{up,down}.sh  # MODIFIED: include the ui daemon
├── .specify/memory/constitution.md  # MODIFIED: v1.3.0 amendment (Node runtime + native non-GPU svc)
└── tests/
    ├── test_stream.py          # NEW: SSE endpoints stream + enforce the key
    ├── test_ui_security.py     # NEW: key never browser-visible; UI bound to 127.0.0.1
    └── test_ui_smoke.*         # NEW: the six surfaces reachable through the UI/BFF
```

**Structure Decision**: The UI is layered alongside the existing native daemons (a peer to
serving/training/vision), the SSE plumbing lives in the gateway (containing the backend change to one
service), and the supervisor + one-command scripts simply gain a fourth managed process. Nothing in
the lifecycle code paths changes.

## Phasing (maps to constitution VII)

- **Phase 0 — Amendment**: ratify constitution **v1.3.0** (Node runtime; native non-GPU localhost
  service). Gate for everything below.
- **Phase 1 — Infer console (US1)**: gateway `/infer/stream`; Next.js app shell + BFF (key injection,
  127.0.0.1); Infer tab (streaming chat + vision); supervisor +ui; `up_all`/`down_all` include it.
  Exit: stream a completion + classify an image from the browser; key never browser-visible.
- **Phase 2 — Health (US2)**: live tiles (`/platform/health` + SSE `state`) + embedded Grafana. Exit:
  daemon death reflects live; panels render.
- **Phase 3 — Models & datasets (US3)**: browse/promote models; upload/browse dataset versions.
- **Phase 4 — Runs (US4)**: launch + live `/runs/{id}/events`; resulting version promotable.
- **Phase 5 — Monitor (US5)**: drift check (REST) + retrain visibility that links into the live Runs
  view (no dedicated monitor SSE — a check returns one report).

Cross-cutting: key-leak + localhost-bind security tests; a no-regression pass of the 001/002 suite.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Next.js + BFF (not a static SPA) | The gateway requires `X-API-Key`; a server-side BFF is the only way to keep the key out of the browser | A static SPA would expose the key or force a UI-only auth exemption on the gateway (re-opens US1's hole) |
| Native WSL Node (not a Compose service) | Disk is the scarcest resource (Principle III); WSL disk is ample, C: is tight | A Node container adds image weight to the C: drive; still needs the Node-runtime amendment anyway |
| Gateway-bridged SSE (not daemon push) | Confines the backend change to one service; daemons keep their simple poll APIs | Native daemon push rewrites all three daemons + their protocols for marginal fidelity |
| Constitution amendment v1.3.0 | Node is a new runtime + a non-GPU native service — both governed clauses | Avoiding the amendment is impossible: any UI introduces the Node runtime; the honest path is to amend |
