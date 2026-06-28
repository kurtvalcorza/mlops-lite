# Feature Specification: Inference Tracing (MLflow)

**Feature Branch**: `006-inference-tracing`

**Created**: 2026-06-28

**Status**: Draft

**Input**: While evaluating `mlflow agent setup` against this repo it became clear the automated path
(`mlflow.autolog()`) captures **nothing** here: the gateway does not call an LLM client library — it
proxies inference over `httpx` to a native llama-server supervisor, and there is no library for autolog
to patch. MLflow is *already* a dependency (`mlflow-skinny==2.18.0`) and already configured as the model
**registry** (`gateway/app/registry.py`, `MLFLOW_TRACKING_URI`). This feature adds the missing,
genuinely valuable piece — **per-request inference traces** — via a small **manual** instrumentation of
the proxy paths, reusing the existing MLflow server.

> **Scope note**: 006 adds **observability only**. It introduces **no new lifecycle stage, no UI
> surface, no new service, and no new dependency** (MLflow is already present). It instruments the
> inference proxy paths so each request emits one MLflow trace (prompt → output, latency, model,
> promoted registry version). Requirement IDs continue the shared space (FR-048+, SC-031+, tasks
> T104+). **No constitution amendment** — Principle VI already mandates MLflow tracking; tracing
> *extends* observability without adding a runtime (see plan.md → Constitution Check).

> **Why not just run `mlflow agent setup`?** Its two install/config steps are already satisfied
> (MLflow installed; tracking URI set), and its core step — `mlflow.autolog()` at the app entry point —
> is a **no-op** for a proxy-based gateway. The value is a targeted `@mlflow.trace` on the inference
> functions, which the automated recipe does not produce. 006 is that targeted change.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Trace the REST inference path (Priority: P1)

Every `POST /infer` request emits exactly **one** MLflow trace, visible in the existing MLflow UI,
recording the prompt, generation parameters, output text, latency (`load_ms` / `infer_ms`), the served
model, token `usage`, the **promoted registry version**, and the final status. The operator can open a
trace and see what was asked, what came back, how long it took, and which registered model version
served it.

**Why this priority**: This is the core gap. The platform already tracks *experiments and model
versions* (Principle VI) but has **no record of individual inferences** — no way to inspect a specific
prompt/response after the fact, correlate latency spikes, or tie a served answer to a model version.
The REST path (`/infer`) is the simplest, highest-value hook and the one the 001 smoke already exercises.

**Independent Test**: With the MLflow server up, issue a `POST /infer`; confirm one trace appears in the
configured experiment with the documented attributes and a `registry_version` matching the promoted
version. No change to the response body or status codes.

**Acceptance Scenarios**:

1. **Given** the MLflow server is reachable, **When** `POST /infer` completes successfully, **Then**
   exactly one trace is recorded with `prompt`, `max_tokens`, `temperature`, `output`, `load_ms`,
   `infer_ms`, `model`, `usage`, `registry_version`, and `status=completed`.
2. **Given** an inference that errors (supervisor unreachable / `ModelTooLargeError`), **When** the
   handler returns its `5xx`/`4xx`, **Then** the trace is still recorded and marked with the error
   (status + exception), so failures are observable too.
3. **Given** a promoted registry version exists for `SERVING_MODEL`, **When** the trace is recorded,
   **Then** `registry_version` equals the version returned by the existing `_resolve_serving_version()`
   (the inference is traceable to a registered, promoted model — FR-006 carried forward).

---

### User Story 2 — Trace the streaming path (Priority: P2)

`POST /infer/stream` (the SSE path the operator UI consumes) emits **one** trace spanning the whole
generation — opened when streaming starts and closed when the last token is sent or the stream errors —
capturing the prompt, parameters, model, and (when the supervisor reports it) the final token/usage
counts, plus any `error` event.

**Why this priority**: The UI's primary inference surface is the stream, not the REST call. Without this,
the most-used path is invisible. It is P2 only because a span over an async generator needs care (close
in a `finally`, never break the SSE framing) — strictly more delicate than the REST wrap.

**Independent Test**: Drive `/infer/stream` from the UI (or curl); confirm one trace spans from first to
last byte, carries the prompt/params/model, and that an induced mid-stream error is recorded on the span
without corrupting the SSE output.

**Acceptance Scenarios**:

1. **Given** the server is reachable, **When** a stream completes, **Then** one trace exists whose
   duration covers the full generation and whose attributes include `prompt`, `max_tokens`,
   `temperature`, and `model`.
2. **Given** the supervisor emits an `error` event mid-stream, **When** the generator ends, **Then** the
   span is closed and marked errored — and the SSE bytes the client receives are byte-for-byte unchanged
   from today (no regression to the passthrough).

---

### User Story 3 — Tracing is best-effort and toggleable (Priority: P1)

Tracing **never** breaks or measurably slows inference. If the MLflow server is unreachable or trace
export fails, the inference still succeeds with no added latency on the GPU-serialized path; and the
operator can disable tracing entirely with a single env flag.

**Why this priority**: Co-equal P1 with US1 — it is the guardrail that makes US1 safe to ship. The
inference path holds the single-GPU mutex (Principle II); a tracing call that blocks on a dead MLflow
server would stall the platform's most critical lock. Fail-open is non-negotiable.

**Independent Test**: Stop the MLflow server; issue `POST /infer` and `/infer/stream` — both succeed with
no meaningful added latency and no error surfaced to the client. Set `MLFLOW_TRACING_ENABLED=0`; confirm
no traces are emitted and no tracing code runs on the request path.

**Acceptance Scenarios**:

1. **Given** the MLflow server is **down**, **When** `POST /infer` runs, **Then** it returns its normal
   `200` with no client-visible error and no significant added latency (export does not block the lock).
2. **Given** `MLFLOW_TRACING_ENABLED=0`, **When** any inference runs, **Then** no trace is emitted and the
   tracing path is fully bypassed.
3. **Given** export retries could otherwise stall, **When** the server is slow/unreachable, **Then** the
   gateway uses fast-fail HTTP settings (`MLFLOW_HTTP_REQUEST_MAX_RETRIES=0`, short timeout) so tracing
   degrades instantly rather than waiting through default retries while holding (or just after holding)
   the GPU lock.

---

### Edge Cases

- **MLflow server down**: inference must succeed unchanged; tracing degrades silently (FR-051). The GPU
  lock must not be held one millisecond longer waiting on a trace export.
- **`mlflow-skinny` tracing at the pinned version** *(resolved)*: verified that `2.18.0` exposes the
  tracing API and that traces persist to the server — no dependency needed (FR-054). The only wrinkle
  is a cosmetic `MlflowSpanProcessor ... '_metrics'` warning, silenced at init.
- **Synchronous export** *(verified)*: MLflow trace export blocks the calling thread (~45 ms healthy,
  up to the HTTP timeout if the server is slow). 006 therefore emits traces from a **background worker**
  off the request path (FR-051) — a refused server fails fast, but a *slow* one must not ride the
  response, hence off-path rather than inline-with-timeout.
- **Streaming span lifecycle**: the span boundary lives outside `_gpu_lock` and the trace is emitted
  from a `finally` even if the client disconnects mid-stream, or spans leak (FR-050).
- **Prompt/response sensitivity**: traces store full prompt + output. Acceptable for the single local
  operator, but capture MUST be disableable (FR-052) for when it isn't.
- **Double-instrumentation**: do **not** add `mlflow.autolog()` — it captures nothing for a proxy
  gateway and risks confusing, empty traces (FR-048).
- **No regression**: all 001–005 tests pass; the six UI tabs and every status code are unchanged
  (SC-035).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-048**: The gateway MUST record inference traces via **manual** MLflow tracing, NOT
  `mlflow.autolog()` (no client library exists to patch on the proxy path). Because the fire-and-forget
  emit (FR-051) must backdate the span to the real request window, it MUST use the **low-level client
  API** — `MlflowClient.start_trace(name, …, start_time_ns=…, experiment_id=…)` then
  `MlflowClient.end_trace(request_id, …, status=…, end_time_ns=…)` — NOT the wall-clock
  `@mlflow.trace` decorator or the fluent `mlflow.start_span` context manager (neither accepts explicit
  timestamps in the pinned version). **Version-alignment note (vs `Projects/mlflow`):** that source is
  MLflow **3.14.x**, whose idiom is the top-level `mlflow.start_span_no_context(start_time_ns=…)` — which
  **does not exist in the gateway's pinned `mlflow-skinny==2.18.0`**. 006 targets the 2.18.0 client API
  above (verified live: a backdated `start_trace`/`end_trace` pair persists with the correct
  `execution_time_ms`). Tracing MUST be initialized **once** at gateway import/startup: reuse the
  existing `MLFLOW_TRACKING_URI` and set a dedicated experiment (default `mlops-lite-inference`) so
  traces are separable from registry/training runs.
- **FR-049**: `POST /infer` MUST emit exactly one trace per request. Idiomatically (MLflow trace UI):
  the `prompt` goes in the trace **`inputs`** and the `output` text in the trace **`outputs`** (both
  gated by `capture_io()`, FR-052); `max_tokens`, `temperature`, `load_ms`, `infer_ms`, `model`
  (`SERVING_MODEL`), `usage`, and `registry_version` (from `_resolve_serving_version()`) go in span
  **`attributes`**; plus a `status`. Error outcomes (`ServingError`, `ModelTooLargeError`, `503`
  health-fail) MUST still produce a trace marked errored (`end_trace(status="ERROR", …)`).
- **FR-050**: `POST /infer/stream` MUST emit one trace spanning the full generation — the span boundary
  lives **outside** `serving._gpu_lock` (timestamp captured before lock acquire; trace emitted from a
  `finally` after lock release, covering completion/error/disconnect) — with `prompt`, `max_tokens`,
  `temperature`, `model`, `status`, an **approximate token count obtained by counting `data:` frames**
  as they pass through, and any `error` event. The gateway MUST NOT parse the SSE content: the
  `aiter_raw` bytes delivered to the client MUST be byte-identical to current behavior. *(Grilled
  decision: frame-count over exact `usage`, to preserve the raw passthrough.)*
- **FR-051**: Tracing MUST be best-effort / fail-open and run **off the request path**. Because MLflow
  trace export is **synchronous** (verified: ~45 ms healthy, up to the HTTP timeout against a slow
  server), the gateway MUST capture trace data inline (cheap, in-process) and perform the span
  build+export on a **background worker** (`asyncio.create_task` → `run_in_threadpool`, not awaited) so
  the inference response never waits on export. A tracing/export failure MUST NOT change the inference
  result, raise to the client, or add latency to the GPU-serialized path. The background task MUST be
  held by a strong reference until done (asyncio keeps only weak refs — an un-referenced fire-and-forget
  task can be GC'd mid-export and silently drop the trace). Fast-fail MLflow HTTP settings
  (`MLFLOW_HTTP_REQUEST_MAX_RETRIES=0`, short `MLFLOW_HTTP_REQUEST_TIMEOUT`) MUST be set as a backstop to
  bound the **background** worker's lifetime. *(Grilled decision: off-path emit, informed by the
  synchronous-export finding.)*
- **FR-052**: An env toggle `MLFLOW_TRACING_ENABLED` (default **enabled**) MUST fully disable tracing
  when falsy — no traces emitted, no per-request tracing overhead. A separate `MLFLOW_TRACE_CAPTURE_IO`
  (default **on**) MUST allow disabling capture of prompt/output bodies while keeping timing/metadata, for
  when prompt content is sensitive.
- **FR-053** *(P3, optional)*: `POST /vision/classify` SHOULD be traced the same way (image size,
  top-k labels, latency, status) for lifecycle-complete observability; deferrable without blocking US1/US2.
- **FR-054** *(RESOLVED 2026-06-28, verified live)*: The pinned `mlflow-skinny==2.18.0` **exposes** the
  tracing API (`mlflow.trace` / `start_span` / `set_experiment`) and a trace **persists** to the
  in-stack MLflow server (`status OK`, span present). **No dependency is added** — `requirements.txt`
  is unchanged; the footprint stays within Principle III. A benign `MlflowSpanProcessor ... '_metrics'`
  warning prints on span-end (cosmetic — the trace still exports); init MUST silence the
  `mlflow.tracing` logger so the gateway logs stay clean.

### Key Entities *(include if feature involves data)*

- **InferenceTrace**: one MLflow trace per inference request — root span `infer` (REST) or
  `infer_stream` (SSE). REST attrs: {prompt, params, output, load_ms, infer_ms, model, exact `usage`,
  registry_version, status}. Stream attrs: {prompt, params, model, status, approximate token count from
  `data:`-frame counting (NOT exact `usage` — see FR-050)}. Lives in the `mlops-lite-inference`
  experiment on the existing MLflow server.
- **TracingConfig**: derived posture from env — `enabled` (`MLFLOW_TRACING_ENABLED`), `capture_io`
  (`MLFLOW_TRACE_CAPTURE_IO`), `tracking_uri` (reused `MLFLOW_TRACKING_URI`), `experiment`
  (`mlops-lite-inference`), and fast-fail HTTP settings.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-031**: A successful `POST /infer` produces exactly one trace in the `mlops-lite-inference`
  experiment with all FR-049 attributes, openable in the existing MLflow UI, with `registry_version`
  matching the promoted version.
- **SC-032**: A `POST /infer/stream` produces one trace spanning first-to-last token; an induced
  mid-stream error is recorded on the span and the client's SSE stream is unchanged.
- **SC-033**: With the MLflow server stopped, `/infer` and `/infer/stream` both succeed with no
  client-visible error and no meaningful added latency (fail-open verified, not assumed).
- **SC-034**: `MLFLOW_TRACING_ENABLED=0` emits zero traces and runs no tracing code on the request path;
  `MLFLOW_TRACE_CAPTURE_IO=0` keeps timing/metadata traces but omits prompt/output bodies.
- **SC-035**: No regression — all 001–005 integration tests pass; every inference status code and the SSE
  framing are unchanged; the single-GPU mutex (Principle II) is never held longer for tracing; the six UI
  tabs behave identically.

## Assumptions

- **MLflow is already present and configured** — `mlflow-skinny==2.18.0` is a gateway dependency and
  `MLFLOW_TRACKING_URI` already points at the in-stack MLflow server (`infra/mlflow`, default
  `http://mlflow:5000`). 006 reuses both; it adds no server and (pending FR-054) no dependency.
- **Single local operator** — full prompt/response capture is acceptable by default; `MLFLOW_TRACE_CAPTURE_IO`
  exists for when it is not. No multi-tenant redaction, no PII pipeline.
- **Proxy architecture stands** — inference remains a gateway→supervisor `httpx` proxy; manual tracing is
  the correct tool because there is no in-process LLM client for autolog to instrument.
- **Observability-only increment** — no lifecycle stage, UI surface, or VRAM-mutex behavior changes;
  purely additive tracing over 002/004/005.

## Non-Goals

- **`mlflow.autolog()` / automated `mlflow agent setup` instrumentation** — a no-op for a proxy gateway;
  explicitly rejected in favor of targeted manual spans (FR-048).
- **Evaluation / scoring code** — no LLM-judge, no eval harness; 006 is tracing only (the `mlflow agent
  setup` rules also forbid adding eval code unprompted). A future `00x-evaluation` may build on these
  traces.
- **New tracing backend or UI** — traces land in the existing MLflow server; no Jaeger/OTLP collector, no
  new dashboard. (An OpenTelemetry export path may be reconsidered later, out of scope here.)
- **Tracing the training/drift paths** — 006 covers inference (and optionally vision); experiment/run
  tracking for training already exists under Principle VI and is unchanged.
- **Trace retention / sampling / pruning** — 006 ships **no** retention policy or sample rate; traces
  accumulate in the `mlops-lite-inference` experiment and the README documents how to prune. Bounded
  storage is revisited only if it bites (Principle III). *(Grilled decision.)*
