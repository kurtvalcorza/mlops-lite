# Contract: `POST` / `GET /models/{name}/shadow-replay` (FR-151)

A new on-demand surface, mirroring 014's `/batch` async shape. Advisory only — it never touches the
promotion gate.

## `POST /models/{name}/shadow-replay`

Dispatch a shadow-replay of a challenger against the champion's logged production traffic.

### Request

```json
{ "challenger": "<version>", "window_n": 100, "modality": "<auto from registry if omitted>" }
```

### Responses

| Case | Response |
|---|---|
| Dispatched | `202 { "shadow_id": "<id>", "status": "queued", "window_n": <n> }` |
| Insufficient data (`< MIN_PAIRS` captured∩labeled) | `200/409 { "status": "insufficient_data", "n_pairs": <n>, "min": <MIN_PAIRS> }` (FR-152) |
| Capture disabled (no corpus) | `409 { "status": "no_corpus", "detail": "QUALITY_CAPTURE_IO is off — no replay inputs" }` |
| Inputs not captured for the modality | `409 { "detail": "inputs not captured for <modality>" }` |

The job runs on the **native trainer** under the single GPU lease (one model resident).

## `GET /models/{name}/shadow-replay/{shadow_id}`

### Response (advisory verdict)

```json
{
  "shadow_id": "<id>",
  "status": "completed",
  "modality": "text-generation",
  "metric": "task_accuracy",
  "direction": "higher",
  "n_pairs": 87,
  "champion": { "version": "<@serving>", "value": 0.91 },     // from logged predictions (no re-run)
  "challenger": { "version": "<v>", "value": 0.88 },          // replayed via 015's scorer
  "winner": "champion",
  "advisory": true                                            // never gates (SC-097)
}
```

## Invariants

- **One model in VRAM** during replay (lease-serialized) — SC-096.
- Champion value comes from **logged predictions**, not a re-run — FR-149.
- The promotion gate (011/015) is **unchanged** — SC-097.
- Same `(input, label)` window + same modality metric/direction for both sides — like-for-like (FR-150).
