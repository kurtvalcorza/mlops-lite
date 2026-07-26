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
| 409 | `gpu_busy` | an exclusive job holds the GPU; inference queued/refused per policy (FR-025) |
| 413 | `model_too_large` | requested model can't fit live free VRAM even after eviction (FR-024) |
| 503 | `metering_unavailable` | ledger write unavailable → GPU work refused (FR-016) |

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
  evicts idle/LRU serving tenants to fit, else `413` (FR-019, FR-023, FR-024).
- While an exclusive job runs, inference returns `409 gpu_busy` or queues per policy (FR-025).

## Metering
Response headers echo `X-GPU-Seconds` (this request) and `X-Quota-Remaining` (window, GPU-seconds).
