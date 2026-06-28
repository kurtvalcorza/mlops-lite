---
description: "Task list for Inference Tracing (006)"
---

# Tasks: Inference Tracing (MLflow)

**Input**: Design documents from `specs/006-inference-tracing/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened platform
(002/004/005). Reuses the existing MLflow server and `MLFLOW_TRACKING_URI`.

**Tests**: Lightweight per-phase smoke/integration tests (constitution VII), run on the target machine
before the next phase. Task IDs continue the shared space (T104+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **COMPLETE & VALIDATED ON HARDWARE.** Built T104–T117 + validated live after
> a gateway rebuild. SC-031: `/infer` trace carries prompt/output + `registry_version="26"` + `usage`,
> status OK. SC-032: `/infer/stream` trace `status OK` with `token_frames` count; SSE bytes intact.
> SC-033: with MLflow **stopped**, `/infer` + `/infer/stream` returned 200 (no client error, no
> trace), and tracing **self-healed** on MLflow restart with no gateway restart (lazy worker-thread
> init). SC-034: 13 offline toggle/fail-open unit checks pass (`MLFLOW_TRACING_ENABLED=0` no-ops;
> `MLFLOW_TRACE_CAPTURE_IO=0` drops bodies). SC-035: full keyed sweep green — the only failure was
> `test_drift_loop` hitting a `409 GPU busy` because serving was VRAM-resident from the 006 inference
> tests (Principle II mutex, NOT a 006 regression); it passes once serving idles out. No constitution
> amendment (advances Principle VI). **Build notes:** init is lazy on the worker thread (one-shot
> import-time init left tracing off when the gateway started before MLflow was ready); the version-aligned
> 2.18.0 `MlflowClient.start_trace`/`end_trace` (explicit ns) is used, not 3.x `start_span_no_context`.
>
> **Pre-flight verified live (2026-06-28, in the running gateway container):**
> - `mlflow-skinny==2.18.0` **exposes** `mlflow.trace` / `start_span` / `set_experiment` AND a trace
>   **actually persists** to the in-stack server (`status OK`, span present) → **FR-054 resolved: NO
>   dependency needed.** (A benign `'MlflowSpanProcessor' object has no attribute '_metrics'` warning
>   prints on span-end — cosmetic; the trace still exports. Silence it at init.)
> - **Trace export is SYNCHRONOUS on the calling thread** (~45 ms against a healthy server; blocks up
>   to `MLFLOW_HTTP_REQUEST_TIMEOUT` against a *slow* one). A *refused* server fails in ~2 ms, but a
>   *slow* one does NOT — so fast-fail HTTP settings alone are insufficient.
>
> **Grilled decisions (2026-06-28):**
> 1. **Span OUTSIDE the GPU lock** (both paths). REST is naturally outside (the lock lives inside
>    `serving.run_inference`, released before the router returns). Streaming: the span wraps `gen()`
>    OUTSIDE `async with serving._gpu_lock`, closed in a `finally` after release → export never runs
>    under the mutex (Principle II safe); span duration includes queue-wait (true end-to-end).
> 2. **Fire-and-forget export OFF the request path** (informed by the sync-export finding). Capture
>    trace data inline (cheap, in-process), then emit+export the span from a **background task**
>    (`asyncio.create_task` → `run_in_threadpool`, never awaited) using the low-level client API with
>    **explicit start/end timestamps**. The inference response never waits on the ~45 ms-to-timeout
>    export — honors SC-033 regardless of MLflow health. Supersedes "inline + fast-fail" framing in
>    FR-051 (fast-fail HTTP stays as a backstop inside the background worker).
> 3. **Streaming trace = metadata + frame count.** Capture prompt/params/model/status/duration plus an
>    approximate token count by counting `data:` frames as they pass — **no SSE content parsing**, the
>    raw `aiter_raw` passthrough stays byte-identical (SC-032). Drops FR-050's "exact final usage."
> 4. **No retention policy in 006** — document trace accumulation + how to prune the
>    `mlops-lite-inference` experiment in the README; revisit only if storage bites (Principle III).

---

## Phase 0 — Dependency pre-flight (FR-054)

- [x] **T104** [US1] **RESOLVED (2026-06-28, verified live):** `mlflow-skinny==2.18.0` exposes
  `mlflow.trace` / `start_span` / `set_experiment` and a trace **persists** to the in-stack server
  (`status OK`). **No dependency added** — `gateway/requirements.txt` unchanged. The benign
  `MlflowSpanProcessor ... '_metrics'` warning on span-end is cosmetic (trace still exports); silence
  the `mlflow.tracing` logger at init.

## Phase 1 — REST tracing (US1) → SC-031

- [x] **T105** [US1] Add `gateway/app/tracing.py`: one-time init (read `MLFLOW_TRACKING_URI`, set
  experiment `MLFLOW_TRACING_EXPERIMENT` default `mlops-lite-inference`, apply fast-fail HTTP env as a
  backstop, silence the cosmetic `mlflow.tracing` `_metrics` warning); expose `enabled()` /
  `capture_io()` toggles and a **fire-and-forget, fail-open emit helper** — `emit(name, inputs, outputs,
  attrs, start_ns, end_ns, status)` that schedules the synchronous span build+export on a background
  worker (`asyncio.create_task(run_in_threadpool(...))`, never awaited), swallowing all tracing errors.
  **Use the 2.18.0 low-level client API with explicit timestamps** — `MlflowClient.start_trace(name,
  inputs=…, attributes=…, start_time_ns=start_ns, experiment_id=…)` then
  `MlflowClient.end_trace(request_id, outputs=…, status=…, end_time_ns=end_ns)` (verified live: backdated
  pair persists with correct `execution_time_ms`). Do **NOT** use `mlflow.start_span_no_context()` — that
  is the MLflow **3.x** idiom in `Projects/mlflow` and is **absent in the gateway's `mlflow-skinny==2.18.0`**;
  nor the `@mlflow.trace`/`mlflow.start_span` wall-clock context managers (they can't backdate). The
  request path never blocks on export. **The helper MUST hold a strong reference to each scheduled task** (a module-level `set()` with
  a `task.add_done_callback(set.discard)` cleanup) — asyncio keeps only *weak* refs to tasks, so a
  fire-and-forget task with no strong ref can be GC'd mid-export and silently drop the trace ("Task was
  destroyed but it is pending"). *(Grilled #2: off-path export, informed by the sync-export finding;
  strong-ref guards the not-awaited task.)* (FR-048, FR-051, FR-052)
- [x] **T106** [US1] In [gateway/app/routers/infer.py](../../gateway/app/routers/infer.py), capture
  the `/infer` trace data inline (record `start_ns` before, `end_ns` after) and call `tracing.emit(...)`
  **after** building the response (the span is at the router, naturally outside `_gpu_lock`): attrs
  `prompt`, `max_tokens`, `temperature`, `output`, `load_ms`, `infer_ms`, `model`, `usage`,
  `registry_version` (from `_resolve_serving_version()`), `status`. The error branches
  (`ModelTooLargeError`, `ServingError`, `503` health-fail) MUST still emit an errored trace. Gate body
  (`prompt`/`output`) capture on `capture_io()`. *(Grilled #1: span outside the lock.)* (FR-049)
- [x] **T107** [P] [US1] `tests/test_tracing_rest.py`: with MLflow up, one `POST /infer` → exactly one
  trace with the FR-049 attributes and a matching `registry_version`; an error inference still produces
  an errored trace. `pytest.skip` when the stack/MLflow/key is absent. (SC-031)

## Phase 2 — Streaming tracing (US2) → SC-032

- [x] **T108** [US2] In [gateway/app/routers/stream.py](../../gateway/app/routers/stream.py), record
  `start_ns` before `async with serving._gpu_lock` and, in a `finally` **outside** that lock block
  (covers completion, error, client disconnect), record `end_ns` and call `tracing.emit(...)` — span
  `infer_stream` with `prompt`/`max_tokens`/`temperature`/`model`/`status`, an **approximate token
  count from counting `data:` frames** as they pass (a cheap counter in the passthrough loop), and any
  `error` event. **No SSE parsing** — `aiter_raw` bytes stay byte-identical to the client. *(Grilled #1
  span-outside-lock + #3 frame-count, not exact usage.)* (FR-050)
- [x] **T109** [P] [US2] `tests/test_tracing_stream.py`: a completed stream → one trace spanning the
  generation with a `data:`-frame token count; an induced mid-stream `error` event is recorded and the
  client SSE byte sequence is **byte-identical** vs the pre-006 baseline. Skip-guarded. (SC-032)

## Phase 3 — Resilience + toggle (US3, co-P1) → SC-033 + SC-034

- [x] **T110** [US3] Verify/realize fail-open: with the MLflow server stopped, `/infer` and
  `/infer/stream` succeed with no client-visible error and a latency delta **within noise** (the
  off-path background emit means the request never waits on export at all — the sync export burns a
  worker thread, not the response). Confirm the fast-fail HTTP backstop
  (`MLFLOW_HTTP_REQUEST_MAX_RETRIES=0`, short `MLFLOW_HTTP_REQUEST_TIMEOUT`) is applied **inside** the
  worker so a slow server bounds the background thread's lifetime (not the request). (FR-051)
- [x] **T111** [US3] Implement the toggles end-to-end: `MLFLOW_TRACING_ENABLED=0` fully bypasses the
  tracing path (no emit, no overhead); `MLFLOW_TRACE_CAPTURE_IO=0` keeps timing/metadata traces but omits
  prompt/output bodies. (FR-052)
- [x] **T112** [P] [US3] `tests/test_tracing_resilience.py`: server-down → inference still `200` and a
  latency delta within noise; `MLFLOW_TRACING_ENABLED=0` → zero traces; `MLFLOW_TRACE_CAPTURE_IO=0` →
  trace present without prompt/output bodies. Skip-guarded. (SC-033, SC-034)

## Phase 4 — (optional) vision + docs → SC-035

- [x] **T113** [P3] [US1] *(optional)* Trace `POST /vision/classify` in
  [gateway/app/routers/vision.py](../../gateway/app/routers/vision.py) (image size, top-k labels,
  latency, status), same fail-open helper. Deferrable. (FR-053)
- [x] **T114** [P] Document the new env in `.env.example`: `MLFLOW_TRACING_ENABLED`,
  `MLFLOW_TRACE_CAPTURE_IO`, `MLFLOW_TRACING_EXPERIMENT`, and the fast-fail HTTP vars.
- [x] **T115** [P] README: a short "Viewing inference traces" note (which experiment, how to open a
  trace in the existing MLflow UI, how to disable tracing) **+ a retention note** — traces accumulate
  in the `mlops-lite-inference` experiment; document how to prune (delete the experiment's traces / the
  experiment) since 006 ships no retention policy (Grilled #4, Principle III). (SC-035-adjacent)
- [x] **T116** Update `GET /` endpoint metadata in [gateway/app/main.py](../../gateway/app/main.py) to
  note `006` tracing in the `phase` string (cosmetic, consistent with prior increments).
- [x] **T117** No-regression sweep: full 001–005 integration suite passes with tracing on; confirm the
  GPU-lock hold time and every inference status code / SSE framing are unchanged. (SC-035)

---

## Dependencies & parallelism

- **T104 (Phase 0) gates everything** — the tracing API must import in the gateway image first.
- **T105 gates T106 and T108** — both routers use the `tracing.py` helper/toggles.
- Within a phase, the `[P]` test files are independent of each other and of the doc tasks.
- **T117 runs last** (needs all instrumentation in place).
- Phase 4 is optional/deferrable; US1–US3 (Phases 0–3) are the shippable core.
