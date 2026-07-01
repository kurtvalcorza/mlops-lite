# Phase 1 Data Model: Shadow-Replay (016)

No new persistent store — rides 013's MinIO `results` bucket + MLflow. Three entities.

## Entity: Captured input (US1)

The recoverable served input for a **sampled** prediction, so a challenger can be re-run over it.

| Field | Notes |
|---|---|
| `prediction_id` | key; ties to 013's `predictions/` + `labels/` records |
| `modality` | text-generation / image-classification / asr (the replayable ones) |
| `input` | recoverable payload — prompt text (LLM), image bytes/b64 (vision), audio bytes/b64 (ASR) |
| `ts` | capture time (for TTL) |

- **Stored**: under a new bounded `inputs/` prefix (or extending the `predictions/` record), **only** with
  `QUALITY_CAPTURE_IO` on and within the **sampled + capped + TTL** policy (D2).
- **Bounding**: sampling rate and/or ring-buffer cap (last N per modality) + TTL prune. Fire-and-forget
  (serving unaffected, FR-119/FR-146).

## Entity: Replay window

The corpus both sides are scored on for one model+modality: the **intersection** of captured inputs and
attached labels over the recent window.

| Field | Notes |
|---|---|
| pairs | `[(input, label)]` = `captured ∩ labeled`, newest `WINDOW_N` |
| `n` | pair count; if `< MIN_PAIRS` ⇒ insufficient data (US3 / FR-152) |
| `model`, `modality` | the champion model + its modality |

- Champion predictions on these pairs are **already logged** (013) → champion-quality reads them (no
  re-run, FR-149).

## Entity: Shadow-replay verdict (advisory)

Per-metric champion (logged) vs challenger (replayed) on the same window.

| Field | Notes |
|---|---|
| `metric`, `direction` | the modality's primary metric (011's registry) |
| `champion_value` | from the champion's logged predictions + labels on the window |
| `challenger_value` | from the challenger replayed over the window (015's scorer) |
| `n_pairs` | window size (provenance) |
| `winner` | advisory per-metric winner (honours direction) |
| `advisory` | always `true` — never gates (FR-150/SC-097) |

- **Persisted**: `results` `shadow/` prefix (and/or an MLflow run). Read by the `GET` endpoint.

## State transition (a shadow-replay job)

```
POST /models/{name}/shadow-replay {challenger}
  → gateway resolves the replay window (captured ∩ labeled), reads champion logged quality
  → if n < MIN_PAIRS → "insufficient data" (no job)        # US3
  → else dispatch trainer job:
       acquire lease → load challenger served artifact → score over the window (015 scorer) → release
  → persist verdict (champion-logged vs challenger-replay)
GET  /models/{name}/shadow-replay/{id} → the advisory verdict
```

See [contracts/shadow-replay-endpoint.md](contracts/shadow-replay-endpoint.md) and
[contracts/capture-extension.md](contracts/capture-extension.md).
