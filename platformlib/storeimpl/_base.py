"""Shared primitives for the store repositories (023 US7 T543).

The store errors + the timestamp/jsonb seam helpers every repository module needs, in one place so
they import from here rather than back through the `store` facade (which would be circular). The
`store` facade re-exports the errors, so `store.StoreError` / `store.LabelExists` are unchanged for
callers, and psycopg stays a lazy per-call import (importing the store must never need the driver).
"""


class StoreError(Exception):
    """The store is unreachable / a read failed. Callers map reads to 502; writes fail-open."""


class LabelExists(StoreError):
    """A label for this prediction_id already exists — the write-once constraint (FR-185)."""


def _json(value):
    """Wrap a Python value for a jsonb column (psycopg needs an explicit adapter for dict/list)."""
    from psycopg.types.json import Json
    return Json(value)


def _dt(epoch):
    """Epoch float (or None) -> a tz-aware datetime for a timestamptz column."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else None


def _epoch(dt):
    """A timestamptz value (or None) -> epoch float, restoring the record's numeric timestamp."""
    return dt.timestamp() if dt is not None else None
