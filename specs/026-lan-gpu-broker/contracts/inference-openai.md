# Contract — Inference Surface (OpenAI-compatible + task-typed CV)

All routes are served by the **gateway**, bound to the LAN interface, over **TLS when more than one tenant
exists** (FR-002a), and require `Authorization: Bearer <tenant-key>`. Each maps to an existing engine child's
`/infer` contract. Usage is **reserved** (estimated) before work and **settled** to actual GPU-seconds on
completion against the calling tenant (FR-016).

## Auth & common errors (all routes)

| Status | Code | When |
|---|---|---|
| 401 | `unauthorized` | missing/invalid/revoked key, or disabled tenant (FR-002) |
| 403 | `quota_exhausted` | tenant's window budget spent (FR-014) |
| 503 | `gpu_busy` | **transient** — an exclusive job holds the GPU, or admission could not fit this model right now (another tenant's outstanding reservation, an unaccounted external consumer, a drain that timed out). Always carries `Retry-After`. (FR-024, FR-025) |
| 413 | `model_too_large` | **permanent** — the estimate exceeds `usable_capacity − safety_headroom`, i.e. the model would not fit even on an **empty** GPU. Never returned for contention (FR-024) |
| 503 | `metering_unavailable` | ledger write unavailable → GPU work refused (FR-016) |

**One code, one status.** `gpu_busy` is **503 + `Retry-After`** on every inference route, whatever caused
it. Earlier revisions of this contract said `409` here while
[admission-scheduler.md](./admission-scheduler.md) annotated the same refusal `503/429` — three codes for
one condition, which a client's retry logic cannot act on. 503 is the accurate one: the condition is
temporary and server-side. `429` would attribute it to the caller's request rate, which is not what
happened; `409` is reserved for the **host agent's** jobs-lane-full contract that `PolicyScheduler` parks
on (FR-182, a different endpoint and a different meaning — "the queue is full", not "the GPU is busy").
Two distinct `code` values share 503; clients branch on `code`, never on the status alone.

**413 vs 503 is permanence, not size.** A model that fits an empty GPU but is blocked right now is
**always** `gpu_busy` — never `413`. Returning 413 for contention tells a client to give up on a request
that would have succeeded seconds later.

## POST /v1/chat/completions  *(chat — LLM)*
OpenAI-compatible request/response (incl. `stream: true` SSE). Maps to the llm child. → FR-005/006/018.

## POST /v1/embeddings  *(embeddings)*
OpenAI-compatible `{model, input[]}` → `{data:[{embedding}]}`. Maps to the embed child. → FR-005.
(CPU-only embeddings hold no GPU lease per Principle II; still metered if GPU-backed.)

## POST /v1/audio/transcriptions  *(ASR — Whisper)*
OpenAI-compatible multipart (`file`, `model`) → `{text}`. Maps to the ASR child. → FR-018.

## POST /v1/vision/{classify|detect}  *(computer vision — task-typed)*
Request `{model, image: <base64|url-on-lan>}` → `classify`: `{labels:[{label,score}]}`;
`detect`: `{objects:[{label,score,box:[x,y,w,h]}]}`. Maps to the vision child. → FR-018.
(No OpenAI schema exists for these; task-typed by design — see research R2.)

## Concurrency & co-residency semantics
- Multiple tenants' requests against a **resident** child are interleaved; none dropped (FR-006, SC-002).
- If the target model isn't resident, admission loads it if it fits the VRAM budget (co-resident), else
  evicts idle/LRU serving tenants to fit. If it still cannot be placed, the answer depends on **why**:
  `413 model_too_large` only when the estimate cannot fit an empty GPU, otherwise `503 gpu_busy` with
  `Retry-After` (FR-019, FR-023, FR-024).
- While an exclusive job runs, inference returns `503 gpu_busy` or queues per policy (FR-025).

## Metering

**Non-streaming** (`stream:false`): response headers echo `X-GPU-Seconds` (this request, settled) and
`X-Quota-Remaining` (window, GPU-seconds). The work is complete before headers are written, so both are
final.

**Streaming** (`stream:true`): headers are flushed *before* the SSE body, and therefore before this
request's GPU-seconds exist. `X-GPU-Seconds` is consequently **not** sent on streamed responses — filling
it would mean either buffering the whole completion (defeating streaming) or labelling an estimate as
settled usage. Final usage arrives as a **terminal SSE event** immediately before `[DONE]`:

```
event: usage
data: {"gpu_seconds": 4.13, "quota_remaining": 1205.9, "window_start": "2026-07-31T00:00:00Z"}

data: [DONE]
```

`X-Quota-Remaining` MAY still be sent on a streamed response as the value **at admission time**, and is
documented as such — it is a pre-flight hint, not a post-settlement figure.
