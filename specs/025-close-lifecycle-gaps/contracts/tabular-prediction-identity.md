# Contract delta — tabular prediction identity (025 US2, T605 / FR-353, FR-359)

An **additive** change to the gateway's `POST /predict` response so a tabular prediction can be labeled
later. Required by FR-359: an external API change lands a contract update (no schema change here, so no
migration — the two obligations are independent).

## What changes: the GATEWAY response only

`POST /predict` (gateway, `gateway/app/routers/tabular.py`) — request **unchanged** (`{"rows": [...]}`).
The response is the child's payload plus two additive fields:

| Field | Type | Meaning |
|---|---|---|
| `prediction_ids` | `[str]` | One id per input row, in row order — the ids minted for this request. |
| `predictions[i].prediction_id` | `str` | The same id, positionally on each prediction object. |

```jsonc
// before
{"model": "tab", "device": "cpu", "features": ["x1"],
 "predictions": [{"prediction": 1, "score": 0.87}]}
// after (additive — every prior field is byte-identical)
{"model": "tab", "device": "cpu", "features": ["x1"],
 "predictions": [{"prediction": 1, "score": 0.87, "prediction_id": "a1b2…"}],
 "prediction_ids": ["a1b2…"]}
```

**Why the id must be returned.** `quality.log_prediction` mints the id internally and the label endpoint
requires a **caller-supplied** id; there is no prediction-list endpoint. Without the id on the response a
tabular prediction is unlabelable, so the delayed-label → quality-window → breach→retrain loop (FR-353,
SC-178) is unreachable. This mirrors the vision route, which already returns `prediction_id`.

Consumers ignore unknown fields (`platformlib.contracts`), so the addition is backward-compatible: an
existing caller that reads only `predictions[i].prediction`/`score` is unaffected.

## The child contract: one additive field

The gateway still mints every prediction id — the child neither mints nor sees one. But the child is
no longer untouched: closing the **T603 warm-reload follow-up** anticipated below made it report the
version it actually scored with, so `specs/020-stack-remediation/contracts/children-api.md` is
amended in the same increment (FR-359).

| Boundary | Added | Meaning |
|---|---|---|
| child `/predict` | `model_version` | the registry version the rows were ACTUALLY scored by — `null` when the registry was unreachable and the env-fallback artifact is resident (never a guess) |

**Why the gateway prefers it over its own registry read.** The `@serving` alias moves the instant a
promote lands, but the child only picks up the new booster on its next version check. Attributing
rows to the registry alone would log predictions produced by the OLD booster under the NEW version,
poisoning that version's quality window with another model's outputs. So `_resolve_tabular_version`
takes the child-reported identity when present and falls back to the registry only for a child too
old to report one — the same agent-reported-identity rule 022 FR-260 established for the LLM path.

This is only sound because the child now RELOADS when the alias moves (previously a warm child served
the old booster indefinitely, since continuous traffic prevents the idle-release that was the sole
re-resolve trigger). Serving side pinned by `tests/test_tabular_serving_version.py`.

## Logged value: the numeric score, not the class

The row logged for quality carries the child's numeric `score` (probability), never the thresholded 0/1
`prediction`: `quality.score_window` feeds stored values straight to `evaluation.auc`, which **ranks**
them, so storing the class would discard exactly the ordering AUC measures. A response from an older
child with no `score` falls back to `prediction` — honest about what was available rather than logging a
class as if it were a probability.

## Failure posture (unchanged)

Logging + capture stay **fail-open, off the response path** (FR-119): ids are minted synchronously and
returned regardless of whether the store write lands; the version resolve + writes run fire-and-forget.
An unreachable/unseeded registry logs `name=None, version=None` rather than failing the prediction.

## Guard

`tests/test_tabular_quality.py` — per-row ids returned positionally, the numeric score logged,
version-scoped attribution, feature-row capture, the empty-predictions no-op, and the
unresolved-version path.
