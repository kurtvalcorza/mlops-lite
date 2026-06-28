# Implementation Plan: Inference Tracing (MLflow)

**Branch**: `006-inference-tracing` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/006-inference-tracing/spec.md`

## Summary

Add per-request MLflow **traces** to the gateway's inference proxy paths via small, manual
instrumentation — not `mlflow.autolog()` (a no-op for a proxy gateway). (US1) wrap the REST `/infer`
path so each request emits one trace with prompt/output/latency/model/registry-version; (US2) wrap the
SSE `/infer/stream` path with a generation-spanning trace closed in a `finally`; (US3, co-P1) make
tracing best-effort/fail-open and toggleable so it never breaks or slows the GPU-serialized path.
Reuse the existing MLflow server and `MLFLOW_TRACKING_URI`. Phase-gated like 002/004/005, validated
live each phase, never regressing 001–005.

## Technical Context

**Language/Version**: Python 3.11 (gateway). No new language.

**Primary Dependencies**: **none added (verified).** `mlflow-skinny==2.18.0` exposes the tracing API
AND a trace persists to the in-stack server (`status OK`) — FR-054 resolved live, no `mlflow-tracing`
or full `mlflow` needed. A cosmetic `MlflowSpanProcessor ... '_metrics'` warning is silenced at init.

**API / version alignment (vs `Projects/mlflow`)**: the local `Projects/mlflow` clone is MLflow
**3.14.x** (dev); its manual-tracing idiom is `mlflow.start_span_no_context(start_time_ns=…)`, which is
**absent in the gateway's pinned `mlflow-skinny==2.18.0`**. 006 therefore uses 2.18.0's low-level client
API — `MlflowClient.start_trace(…, start_time_ns=…)` + `end_trace(…, end_time_ns=…)` — verified live to
produce a correctly-timed, persisted trace from a *backdated* (fire-and-forget) emit. Do not lift 3.x
patterns from `Projects/mlflow` into the gateway without checking they exist in 2.18.0.

**Storage**: none new. Traces persist to the existing in-stack MLflow server (`infra/mlflow`).

**Target Platform**: Win11 + WSL2 + Rancher Desktop. Tracing runs inside the gateway container, which
already reaches MLflow at `MLFLOW_TRACKING_URI`.

**Project Type**: observability-only increment over 002/004/005 — touches a new `gateway/app/tracing.py`,
`gateway/app/routers/infer.py`, `gateway/app/routers/stream.py`, optionally `gateway/app/routers/vision.py`,
`.env.example`, and README. `gateway/requirements.txt` and `gateway/app/serving.py` stay **unchanged**
(FR-054 resolved: no dep; tracing lives in the routers, not the transport client).

**Performance Goals**: zero meaningful added latency on the inference path. **Verified caveat:** MLflow
export is *synchronous* (~45 ms healthy, up to the HTTP timeout if slow), so 006 captures data inline
and emits the span from a **background worker** (`asyncio.create_task` → `run_in_threadpool`), keeping
both the response and the event loop free (FR-051). The single-GPU mutex (Principle II) is never held
for tracing — the span sits outside `_gpu_lock` on both paths.

**Constraints**: no lifecycle change, no UI surface, no new service; the SSE passthrough bytes stay
byte-identical; fail-open is non-negotiable; footprint stays within Principle III.

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Traces go to the in-stack MLflow server; nothing leaves the host | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | Tracing is read-only observability; spans add negligible in-process time and export is fail-fast/off-path — the VRAM mutex is never held longer | ✅ unchanged |
| III. Lightweight Footprint | No new service; no new dependency (FR-054 resolved live — `mlflow-skinny==2.18.0` already exposes the tracing API) | ✅ |
| IV. Full Lifecycle Coverage | Strengthens the *serving/monitoring* stage's observability; adds/drops no stage | ✅ strengthened |
| V. OSS & Swappable | MLflow is OSS and already in use; traces use the standard MLflow tracing API | ✅ |
| VI. Reproducibility & Observability | **Directly advances** — extends mandated MLflow tracking from experiments/models to individual inferences | ✅ strengthened |
| VII. Phase-Gated Delivery | Three independently-runnable stories (US1–US3), each verifiable on the target machine | ✅ |
| Workflow: "no new runtime without amendment" | None introduced | ✅ no amendment |

**No amendment required.** 006 operates within the existing constitution and advances Principle VI;
the only dependency question (FR-054) resolves to a *lighter* MLflow package at most, never a new
runtime. Clean gate-check, mirroring 005.

## Project Structure

### Source Code (delta over 005)

```text
mlops-lite/
├── gateway/app/
│   ├── tracing.py                # NEW: one-time init (tracking URI + experiment + fast-fail HTTP +
│   │                             #      silence the cosmetic _metrics warning); enabled/capture-io
│   │                             #      toggles; a fire-and-forget, fail-open emit(name, attrs,
│   │                             #      start_ns, end_ns, status) that backgrounds the sync export
│   ├── serving.py                # UNCHANGED: router approach chosen (see below) — serving.py stays a
│   │                             #      pure transport client (option A would decorate run_inference)
│   ├── routers/
│   │   ├── infer.py              # MODIFIED: wrap /infer in a fail-open span; set FR-049 attributes
│   │   ├── stream.py             # MODIFIED: span the generation in gen(), close in finally (FR-050)
│   │   └── vision.py             # MODIFIED (P3, optional): trace /vision/classify (FR-053)
├── gateway/requirements.txt      # UNCHANGED (FR-054 resolved: no dep)
├── .env.example                  # MODIFIED: document MLFLOW_TRACING_ENABLED, MLFLOW_TRACE_CAPTURE_IO,
│                                  #           MLFLOW_TRACING_EXPERIMENT, fast-fail HTTP vars
├── README.md                     # MODIFIED: "viewing inference traces" note
└── tests/
    ├── test_tracing_rest.py      # NEW: /infer emits one trace w/ attributes; error path traced
    ├── test_tracing_stream.py    # NEW: /infer/stream spans generation; SSE bytes unchanged
    └── test_tracing_resilience.py# NEW: server-down fail-open; MLFLOW_TRACING_ENABLED=0 bypass
```

**Structure Decision**: put the init + fail-open helper in a dedicated `tracing.py` so the toggle,
fast-fail config, and "never raise" wrapper live in one place and the routers stay readable.

**Trace at the router, not in `serving.py` (decided)**: the router already holds the full picture
(prompt, params, the `run_inference` result, and `registry_version` from `_resolve_serving_version()`),
and it owns the error mapping. Tracing there keeps `serving.py` a pure transport client and captures
the registry version without threading it down. (Option A — `@mlflow.trace` on `run_inference` — is
simpler but can't see `registry_version` and double-wraps the streaming path; kept only as a documented
fallback if a later non-gateway caller ever needs the span. Chosen for 006: the router approach.)

## Phasing (maps to constitution VII)

- **Phase 0 — Dependency pre-flight (FR-054)**: **DONE (verified live)** — `mlflow-skinny==2.18.0`
  exposes the tracing API and traces persist to the server; no dependency added. Export confirmed
  synchronous → Phases 1–3 use the background emit helper.
- **Phase 1 — REST tracing (US1)**: `tracing.py` init + fail-open helper; wrap `/infer`; attributes per
  FR-049; error path traced. Exit: SC-031.
- **Phase 2 — Streaming tracing (US2)**: span `gen()` in `stream.py`, close in `finally`, capture
  error events; prove SSE bytes unchanged. Exit: SC-032.
- **Phase 3 — Resilience + toggle (US3, co-P1)**: fail-open verified with MLflow down; fast-fail HTTP
  settings; `MLFLOW_TRACING_ENABLED` + `MLFLOW_TRACE_CAPTURE_IO`. Exit: SC-033 + SC-034.
- **Phase 4 — (optional) vision + docs**: trace `/vision/classify` (FR-053); `.env.example` + README;
  no-regression sweep. Exit: SC-035.

Cross-cutting: a no-regression pass of the full 001–005 suite with tracing on, and a timing check that
the GPU lock hold time is unchanged.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Manual `@mlflow.trace` instead of `mlflow.autolog()` | The gateway proxies over `httpx`; there is no in-process LLM client for autolog to patch, so autolog captures nothing | `autolog()` is "less code" but produces zero traces here — it solves nothing for a proxy gateway |
| Trace in the router, not `serving.py` | The router sees prompt + result + `registry_version` + error mapping in one place; keeps `serving.py` a pure transport client | Decorating `run_inference` can't see the registry version and double-wraps the stream path |
| Background fire-and-forget emit (off the request path) | **Verified:** MLflow export is synchronous (~45 ms healthy, up to the timeout if slow) — inline export would add that to every inference response. Backgrounding it keeps the response (and the GPU lock, already released) free | Inline + short timeout still adds ~45 ms to every response and up to the timeout when MLflow is slow-but-reachable; a refused server fails fast but a *slow* one doesn't — fails SC-033 |
| Span outside `_gpu_lock` (both paths) | Export (even sync, on a worker) must never coincide with holding the single-GPU mutex; REST is naturally outside (lock is inside `run_inference`), streaming wraps the boundary outside `async with _gpu_lock` | Span inside the lock could let a tracing finalize touch the mutex window — unacceptable on Principle II's most critical lock |
| Frame-count token approx (no SSE parse) | The stream is a byte-identical `aiter_raw` passthrough; counting `data:` frames is O(1) per chunk and preserves the exact bytes | Parsing the SSE to extract exact `usage` adds work to the hot path and risks the byte-identical guarantee (SC-032) |
| Fail-open + fast-fail HTTP backstop | Even off-path, a slow MLflow shouldn't pile up background workers; retries=0 + short timeout bounds each worker's lifetime | "Just call mlflow" risks raising to the client or piling up threads when MLflow is down — unacceptable on the P1 path |
| Dedicated `tracing.py` module | Centralizes the enable/capture toggles, init, and never-raise wrapper so routers stay clean and the fail-open contract is in one tested place | Inlining init+guards in each router scatters the fail-open contract and invites a path that forgets to swallow errors |
| No new dependency (FR-054 resolved live) | `mlflow-skinny==2.18.0` already exposes the tracing API and traces persist — verified, nothing added | Adding `mlflow-tracing` or full `mlflow` would have spent Principle III footprint for no gain once skinny proved sufficient |
