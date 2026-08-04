"""Trace normalization (027 T745 — data-model §8, FR-412/413).

Spans arrive from the tracking server in whatever shape its client version produces. They leave here
as a **generic span tree**: id, parent, name, start, duration, attributes, events, status. That
normalization happens server-side so FR-413 is enforced in one place rather than in every component
that draws a waterfall, and so the client never learns a tracking-vendor payload shape — the
swappability seam Principle V asks for.

**No token-oriented assumptions** (FR-413). It is tempting to render a trace as prompt → tokens →
completion, because that is what an LLM trace looks like and LLM traces are the ones people look at
most. But this platform serves five modalities: an image classification has no tokens, an embedding
call has no completion, and a tabular prediction has neither. A waterfall that assumed them would
render three of the five modalities as broken traces. So the tree carries duration and nesting,
which every modality has, and anything token-shaped stays where it belongs — in `attributes`, under
the name the producer gave it.

Uses the already-pinned `mlflow-skinny` client (research R6). **No new dependency.**
"""


def normalize(trace) -> dict:
    """A `TraceDetail` from a tracking-server trace object or dict."""
    info = _attr(trace, "info", {}) or {}
    spans = [_span(s) for s in (_attr(trace, "data", {}) or {}).get("spans", [])
             or _attr(trace, "spans", []) or []]

    # Relative to the earliest span, so a waterfall starts at zero rather than at an epoch. Absolute
    # timestamps stay in `attributes` for anyone correlating with a log line.
    origin = min((s["startMs"] for s in spans), default=0.0)
    for span in spans:
        span["startMs"] = round(span["startMs"] - origin, 3)

    total = max((s["startMs"] + s["durationMs"] for s in spans), default=0.0)
    return {
        "traceId": _get(info, "trace_id") or _get(info, "request_id"),
        "predictionId": (_get(info, "tags") or {}).get("prediction_id"),
        "modelVersion": (_get(info, "tags") or {}).get("registry_version"),
        "totalDurationMs": round(total, 3),
        "spans": sorted(spans, key=lambda s: (s["startMs"], s["name"])),
    }


def _span(span) -> dict:
    start_ns = _attr(span, "start_time_ns", None) or _get(span, "start_time_ns") or 0
    end_ns = _attr(span, "end_time_ns", None) or _get(span, "end_time_ns") or start_ns
    status = str(_attr(span, "status", "") or _get(span, "status") or "").upper()

    return {
        "spanId": _attr(span, "span_id", None) or _get(span, "span_id"),
        "parentSpanId": _attr(span, "parent_id", None) or _get(span, "parent_id"),
        "name": _attr(span, "name", None) or _get(span, "name") or "span",
        "startMs": start_ns / 1e6,
        "durationMs": max(0.0, (end_ns - start_ns) / 1e6),
        # Kept as an opaque bag. Whatever token counts, prompts, or vendor-specific fields a producer
        # attaches live here under their own names — the tree above them stays modality-agnostic.
        "attributes": dict(_attr(span, "attributes", None) or _get(span, "attributes") or {}),
        "events": [{"name": _get(e, "name"), "timeMs": (_get(e, "timestamp") or 0) / 1e6}
                   for e in (_attr(span, "events", None) or _get(span, "events") or [])],
        "status": "error" if "ERROR" in status else "ok",
        "error": _get(span, "error"),
    }


def depth_of(spans) -> dict:
    """Nesting depth per span id, for the waterfall's indentation.

    Computed here rather than in the component because it is a property of the tree, and two
    components computing it independently is two chances to disagree about what a cycle means.
    """
    by_id = {s["spanId"]: s for s in spans}
    depths = {}

    def depth(span_id, seen):
        if span_id in depths:
            return depths[span_id]
        span = by_id.get(span_id)
        parent = span and span.get("parentSpanId")
        # A malformed trace can contain a cycle. Treat a repeat as a root rather than recursing:
        # a rendering that hangs is worse than one that shows a span at the wrong indentation.
        if not parent or parent in seen or parent not in by_id:
            depths[span_id] = 0
        else:
            depths[span_id] = depth(parent, seen | {span_id}) + 1
        return depths[span_id]

    for span in spans:
        depth(span["spanId"], set())
    return depths


def _attr(obj, name, default):
    return getattr(obj, name, default) if not isinstance(obj, dict) else obj.get(name, default)


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
