"""Prediction records, payload previews, captures, and the review queue (027 T742/T743/T744).

**Payloads are hidden by default, structurally.** The list and detail projections build a
`PayloadPreview` **without** a `preview` field — the content is not merely styled away, it is never
sent. A component cannot accidentally render a payload it was never given, which is what makes the
guarantee hold under refactoring rather than depending on every future contributor remembering the
convention (FR-408).

Revealing is a separate, explicit call, and it is a `POST` with the identifier in the **body**
specifically so no payload reference lands in a URL. URLs reach access logs, browser history, and
`Referer` headers; a `GET /predictions/{id}/payload` would put a pointer to sensitive content in all
three, permanently, for every reveal anyone ever performs (SC-192).

Records come from the **gateway's own prediction table**, never reconstructed from traces (FR-407).
Traces are sampled and lossy by design; a prediction list built from them would silently omit the
predictions that were not traced, and "the request I am looking for is missing" is the worst possible
failure for an audit surface.
"""

#: `PredictionRecord.captureState` / `.labelState`, data-model §8.
CAPTURE_STATES = ("not-captured", "captured", "expired", "sampled-out")
LABEL_STATES = ("unlabeled", "pending-review", "labeled", "disputed", "excluded")

#: Bytes shown on an explicit reveal before truncating. Large payloads truncate with the **true**
#: size stated rather than loading the whole object: a multi-megabyte transcript rendered into a
#: browser panel is a page that stops responding, and the operator wanted the shape, not the bytes.
PREVIEW_LIMIT_BYTES = 4096


def prediction_record(row, *, capture_state=None, label_state=None):
    """One `PredictionRecord` from a gateway prediction row."""
    return {
        "id": row.get("prediction_id"),
        "timestamp": row.get("served_at"),
        "endpointId": row.get("endpoint_id"),
        "modelName": row.get("model_name"),
        "registryVersion": row.get("version"),
        "modality": row.get("modality"),
        # A row exists only because a response was produced — `quality.log_prediction` is called on
        # the success path, and the table has no error column. So `ok` here is a fact about what was
        # recorded, not an assumption: a failed request leaves no row at all, which is a real gap in
        # the record and one an operator should know about rather than read as "nothing failed".
        "status": "error" if row.get("error") else "ok",
        "latencyMs": row.get("latency_ms"),
        "captureState": capture_state or ("captured" if row.get("payload_ref") else "not-captured"),
        "labelState": label_state or "unlabeled",
        "traceId": row.get("trace_id"),
        "policyResult": row.get("policy_result"),
        "error": row.get("error"),
    }


def payload_preview(*, available, total_bytes=None, redacted_fields=None, content=None,
                    revealed=False):
    """A `PayloadPreview`. **`preview` is present only on an explicit reveal.**

    Note what this function does NOT do when `revealed` is false: it does not set `preview` to
    `None`, or to an empty string, or to a placeholder. The key is absent. A key that is present but
    empty is one `??` away from being filled in by a well-meaning change; an absent key makes the
    omission visible in the payload itself.
    """
    preview = {
        "available": bool(available),
        "revealed": bool(revealed),
        "truncated": False,
        "totalBytes": total_bytes,
        "redactedFields": list(redacted_fields or []),
    }
    if not revealed:
        return preview

    body = content if isinstance(content, str) else (content or b"").decode("utf-8", "replace")
    if len(body.encode("utf-8")) > PREVIEW_LIMIT_BYTES:
        body = body.encode("utf-8")[:PREVIEW_LIMIT_BYTES].decode("utf-8", "ignore")
        preview["truncated"] = True
    preview["preview"] = body
    return preview


# -- review queue ------------------------------------------------------------------------------------

#: FR-411, in priority order. The ordering is the design: a prediction a policy already flagged is
#: more worth an operator's attention than one that merely happens to be unlabeled, and a queue
#: sorted by arrival time would bury the first behind a thousand of the second.
PRIORITY_SIGNALS = ("policy-flagged", "low-confidence", "drift-contributor", "sampled",
                    "missing-label", "manually-flagged", "suggested")

#: **What this deployment can actually produce today.** The `predictions` and `capture_index` tables
#: (001 baseline) record `prediction_id, model_name, version, modality, served_at, streamed,
#: payload_ref` and `input_ref, captured_at` — there is no per-prediction confidence, sampling flag,
#: drift attribution, or manual flag anywhere in the schema.
#:
#: So five of the seven signals above are **unreachable on this platform**, and that is stated here
#: rather than left as branches that quietly never fire. A ranking function full of dead conditions
#: reads as a working prioritizer; the surface reports this set so it can say what it is actually
#: ranking by, which is the same rule as the admission "queue" that never queues. `PRIORITY_SIGNALS`
#: stays whole because it is the contract's vocabulary — when a column arrives, the signal lights up
#: and this set is what changes.
DERIVABLE_SIGNALS = ("policy-flagged", "missing-label")

_SIGNAL_WEIGHT = {signal: index for index, signal in enumerate(PRIORITY_SIGNALS)}


def review_queue(rows, *, limit=100):
    """Prioritized review items (FR-411).

    Every item states **which** signal put it there. A queue that ranks without saying why is a
    queue an operator has to take on faith, and the first time the ordering surprises them they
    stop trusting all of it.
    """
    items = []
    for row in rows:
        signals = _signals_for(row)
        if not signals:
            continue
        items.append({
            "predictionId": row.get("prediction_id"),
            "modality": row.get("modality"),
            "modelName": row.get("model_name"),
            "labelState": row.get("label_state") or "unlabeled",
            "signals": signals,
            "reason": signals[0],
            "capturedAt": row.get("captured_at") or row.get("served_at"),
        })

    items.sort(key=lambda item: (_SIGNAL_WEIGHT.get(item["reason"], 99),
                                 -(item["capturedAt"] or 0)))
    return items[:limit]


def _signals_for(row):
    signals = []
    if row.get("policy_result") not in (None, "ok", "pass"):
        signals.append("policy-flagged")
    confidence = row.get("confidence")
    if confidence is not None and confidence < 0.6:
        signals.append("low-confidence")
    if row.get("drift_contributor"):
        signals.append("drift-contributor")
    if row.get("sampled"):
        signals.append("sampled")
    if not row.get("label"):
        signals.append("missing-label")
    if row.get("flagged"):
        signals.append("manually-flagged")
    if row.get("suggested"):
        signals.append("suggested")
    signals.sort(key=lambda s: _SIGNAL_WEIGHT.get(s, 99))
    return signals
