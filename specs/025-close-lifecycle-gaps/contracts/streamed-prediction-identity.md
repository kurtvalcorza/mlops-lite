# Contract delta — streamed prediction identity (025 US4, T610 / FR-356, FR-359)

An **additive** change to `POST /infer/stream`: one leading metadata frame so a streamed prediction can be
labeled. This amends 022's byte-compatibility statement, which is why it is written down (FR-359: an
external API change lands a contract update; no schema change here, so no migration — the obligations are
independent).

## What changes

`POST /infer/stream` (gateway, `gateway/app/routers/stream.py`) — request **unchanged**. The response
stream now opens with exactly ONE added SSE frame, before the supervisor's own frames:

```jsonc
data: {"event": "metadata", "prediction_id": "a1b2…", "model": "qwen-ft", "version": "5"}

// …then the supervisor's frames, byte-for-byte unchanged:
data: {"event": "start"}
data: {"event": "token", "text": "hel"}
data: {"event": "done"}
```

| Field | Type | Meaning |
|---|---|---|
| `prediction_id` | `str` | The id the completed prediction is logged under — pass it to the label endpoint. |
| `model` / `version` | `str` | The agent-reported served identity (022 FR-261/262), the same one the row is attributed to. |

## Amends 022's byte-compatibility claim (FR-271)

`specs/022-registry-driven-llm-serving/contracts/serving-resolution.md` states that `POST /infer` and
`POST /infer/stream` request/response shapes are unchanged. That remains true for `/infer` and for **every
supervisor frame** on the stream; it is now **deliberately amended for the stream's opening frame only**.

The distinction that keeps this safe: the added frame is *prepended*, never a modification of an existing
frame, so the passthrough guarantee the trace/token-count logic depends on is intact. A client that
ignores unknown `event` values (the SSE convention this stack already relies on) is unaffected; a client
that assumed the FIRST frame is `start` must now skip a leading `metadata`.

## Why the id must be on the wire

`quality.log_prediction` mints the prediction id internally, the label endpoint requires a
**caller-supplied** id, and there is no prediction-list endpoint. So before this change a streamed
prediction was logged but **permanently unlabelable** — the delayed-label → quality-window loop (SC-180)
worked for every non-streamed path and silently could not work for streaming. Returning the id is the
minimum that closes it, and it mirrors what `/infer` and the vision/tabular routes already do.

## Labelability is tied to completion (unchanged rule)

The id is delivered up front, but the prediction row is written only when the stream **completes** — the
pre-existing `outcome == "completed"` rule. So on a truncated/aborted/errored stream the client holds an
id with no row behind it, and attaching a label to it fails as an unknown prediction. That is the honest
behavior (nothing was successfully served), and it is asserted in the guard tests rather than left
ambiguous.

Streamed rows still carry no captured output (`prediction=None` → the store marks them `streamed`), so
they stay excluded from champion scoring exactly as before; capture of streamed prompts remains OFF (016
FR-146 — it would evict replayable REST inputs from the bounded ring buffer).

## Guard

`tests/test_stream_capture.py` — the metadata frame is first and carries a per-request id, the logged row
uses that same id, **the supervisor's frames are byte-identical and exactly one frame is added**, and the
truncated/upstream-error paths log nothing.
