"""Paged, filtered journal reads (027 T710 — contracts/runtime-api.md `GET /journal`).

A read-side companion to `hostagent/journal.py`, kept separate so the durable write path — which is
what job correctness depends on — gains no read-shaped code it does not need.

**Paging is mandatory; there is no "all" mode.** Two independent reasons, and either alone would be
enough: the journal grows without bound across a machine's life, and the agent transport's 1 MiB JSON
cap (023 US6) would fail an unbounded response anyway. Paging here is a correctness requirement, not
a nicety, so the cap is enforced rather than defaulted.

**`checksum_state` is surfaced honestly.** A `torn` tail entry is **shown as torn**, never silently
dropped. A missing final transition is exactly what an operator investigating a crash needs to see,
and a reader that quietly discards it turns the most informative record in the file into an absence.
"""
import datetime

#: Contract defaults. The hard cap is enforced, not merely documented — a caller asking for 10 000
#: entries gets 500, because the transport would reject the alternative anyway.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _iso(epoch):
    if epoch is None:
        return None
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_cursor(cursor):
    """`seq:<n>` -> n. A malformed cursor reads as "from the top" rather than raising: a client that
    round-trips a cursor it did not understand should get the first page, not an error page."""
    if not cursor:
        return None
    text = str(cursor)
    if text.startswith("seq:"):
        text = text[4:]
    try:
        return int(text)
    except ValueError:
        return None


def _as_entries(records) -> list:
    """Job records -> journal entries, newest-first, with a synthetic stable sequence.

    The journal's durable rows are job records rather than an append-only event log, so `sequence` is
    derived from the record's own ordering key. It is stable for a given record set and monotonic in
    time, which is all a cursor needs; it is deliberately NOT presented as a durable log offset,
    because it is not one.
    """
    entries = []
    for record in records:
        when = record.get("ended_at") or record.get("started_at") or record.get("submitted_at") or 0
        entries.append({
            "sequence": int((when or 0) * 1000),
            "timestamp": _iso(when),
            "event_type": "transition",
            "job_id": record.get("job_id"),
            "engine_id": record.get("modality") or None,
            "pid": record.get("pid"),
            "device_index": record.get("device_index", 0),
            "from_state": record.get("from_state"),
            "to_state": record.get("state"),
            "detail": record.get("reason"),
            # `ok` unless the record itself is missing the transition stamps its state implies —
            # a `running`/terminal record with no `started_at` is the shape a torn tail leaves.
            "checksum_state": _checksum_state(record),
        })
    entries.sort(key=lambda e: e["sequence"], reverse=True)
    return entries


def _checksum_state(record) -> str:
    state = record.get("state")
    if state in ("running", "succeeded", "failed", "cancelled", "interrupted") \
            and not record.get("started_at"):
        return "torn"
    if state in ("succeeded", "failed", "cancelled") and not record.get("ended_at"):
        return "torn"
    return "ok"


def read(journal, *, cursor=None, limit=DEFAULT_LIMIT, job_id=None, engine_id=None,
         event_type=None, since=None, until=None) -> dict:
    """One page of the journal, newest-first.

    Filters are applied **before** paging, so `limit` counts matching entries rather than scanned
    ones — a filter that returned a short page because most of the window was filtered out would
    make `has_more` meaningless.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    entries = _as_entries(journal.jobs())

    if job_id:
        entries = [e for e in entries if e["job_id"] == job_id]
    if engine_id:
        entries = [e for e in entries if e["engine_id"] == engine_id]
    if event_type:
        entries = [e for e in entries if e["event_type"] == event_type]
    if since is not None:
        entries = [e for e in entries if (e["sequence"] or 0) >= float(since) * 1000]
    if until is not None:
        entries = [e for e in entries if (e["sequence"] or 0) <= float(until) * 1000]

    after = _parse_cursor(cursor)
    if after is not None:
        entries = [e for e in entries if e["sequence"] < after]

    page = entries[:limit]
    has_more = len(entries) > limit
    return {
        "entries": page,
        "next_cursor": f"seq:{page[-1]['sequence']}" if page and has_more else None,
        "has_more": has_more,
    }
