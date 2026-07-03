# Contract: GPU Host Agent API (single stable endpoint)

Base: `http://<host>:8100` (R3; binds `AGENT_BIND`, default `0.0.0.0` so the gateway/Prometheus
containers reach it via host-gateway — from the WSL host itself it's `127.0.0.1:8100`). All
inference traffic flows through here (clarify Q1);
child endpoints never leak. Error vocabulary preserved from 008–017: 400 bad input, 409
busy/refused, 502 engine failure, 503 engine unavailable/unreachable, 507 VRAM exceeded.

## Read surface (open, like today's probes)

- `GET /health` → `{ok, engines: {id: state}, gpu: {free_gb, holder, wedged}, jobs_active,
  interrupted_since_start}` — served from cache (R1); never forks; never blocks on a busy lock.
- `GET /metrics` — Prometheus text; direct scrape target (FR-174): gpu gauges, per-engine
  state/latency/load counters, job states, journal alerts, dropped-work counters.
- `GET /engines` → engine list incl. `unavailable(reason)` (R7).

## Inference passthrough (per engine)

- `POST /engines/llm/infer` (+ `POST /engines/llm/infer/stream` SSE)
- `POST /engines/vision/classify` · `POST /engines/asr/transcribe`
- `POST /engines/embed/embed` · `POST /engines/tabular/predict` (CPU: no admission)

Request/response bodies are **byte-compatible** with today's daemon surfaces (FR-177) — the
gateway's routers change base URL only. Each GPU call runs: ensure-admitted (cold-load if
needed) → forward to child → stamp `last_used`. `preempt=true` semantics per `swap` below.
Busy → 409 `{holder, kind}`; too large → 507; child failure → 502.

## Swap & unload control (state-changing: opt-in `X-Agent-Control` secret, R6)

- `POST /control/unload` `{engine, drain_timeout_s}` → drains then unloads the resident tenant
  (idle-release path made operator-invokable; replaces per-daemon `unload-now`).
- Preempt path (internal to inference calls carrying `preempt=true`): under the **single
  admission lock** — refuse if holder kind is `job` (FR-172, no probe) → drain holder (bounded)
  → unload → admit target → load (FR-171). No release-then-race window exists by construction.

## Jobs

- `POST /jobs` `{kind, modality, request}` → `202 {job_id}`; 409 if a job slot or GPU conflict
  (same semantics the trainer returns today).
- `GET /jobs/{id}` → JobRecord; `GET /jobs?kind=…` → listing (journal-backed, restart-proof).
- `POST /jobs/{id}/cancel` (control-secret) → best-effort terminate of the run subprocess.

Legacy trainer routes (`/train`, `/study`, `/batch`, `/shadow-replay`) are served as aliases
during the jobs fold-in phase, then removed from the gateway's call sites in the same phase.

## Migration interop

While any legacy daemon remains, the agent acquires/releases the **lockfile** for its own
tenants (FR-166) — its in-process lock nests inside lockfile ownership, so cross-boundary
mutual exclusion holds. The interop shim and this clause are deleted at retirement.
