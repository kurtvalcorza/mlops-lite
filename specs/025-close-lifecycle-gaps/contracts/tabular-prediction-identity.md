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

## What does NOT change: the child contract

`specs/020-stack-remediation/contracts/children-api.md` describes the **tabular child's** `/predict` as
`JSON (rows)` → `JSON (predictions)`. That row is still accurate and needs no edit: the child is
untouched by this slice. The gateway adds the identity fields **on top of** the child's response; the
child neither mints nor sees a prediction id.

(If a later slice makes the child report its own registry version — the T603 warm-reload follow-up — that
WOULD change the child response and require a `children-api.md` edit at that time.)

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
