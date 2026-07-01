# Contract: 013 input-capture extension (FR-146/147)

Extends 013's `quality.log_prediction` to store a **recoverable** input (not just a hash) for a **bounded
sample** of predictions, so a challenger can be replayed over real traffic. Behind the existing opt-in.

## Behavior

`log_prediction(...)` (gateway, fire-and-forget, fail-open — unchanged contract for serving) additionally,
**when `QUALITY_CAPTURE_IO` is on AND the sampling/cap policy admits this prediction**, stores a recoverable
input keyed by `prediction_id`.

| Modality | Recoverable input captured | Was (013) |
|---|---|---|
| text-generation | the prompt (already passed as `input_ref`) | already captured |
| image-classification | the image bytes/b64 | **only a SHA hash** → now recoverable |
| asr | the audio bytes/b64 | nothing replayable → now recoverable |

## Config (operator-settable — FR-147)

| Env | Meaning | Default |
|---|---|---|
| `QUALITY_CAPTURE_IO` | master opt-in (existing) — off ⇒ **no** recoverable input stored | on* |
| `SHADOW_CAPTURE_SAMPLE` | fraction of predictions whose input is captured | e.g. `1.0` LLM / lower for image/audio |
| `SHADOW_CAPTURE_CAP_N` | ring-buffer cap: keep at most the last N captured inputs per modality | e.g. `500` |
| `SHADOW_CAPTURE_TTL_S` | retention TTL for captured inputs | e.g. `7d` |

\* privacy note: capturing full prompts/images/audio is sensitive — operators set the policy; off ⇒ no
corpus ⇒ shadow-replay reports `no_corpus`.

## Invariants

- **Serving is never affected** — capture is fire-and-forget + fail-open (FR-119/FR-146), like 013 logging.
- **Bounded storage** (Principle III) — sample + cap + TTL prune; older captures evicted.
- **Privacy default preserved** — `QUALITY_CAPTURE_IO` off ⇒ nothing recoverable stored (SC-094).
- Storage rides the MinIO `results` bucket (new `inputs/` prefix or an extension of the `predictions/`
  record — pinned in tasks).
