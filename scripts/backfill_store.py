#!/usr/bin/env python3
"""One-shot backfill of pre-US4 MinIO objects into the gateway Postgres store (018 T376, FR-187).

Before US4 the monitoring/job state rode MinIO objects; T373–T375 cut the live paths over to the
resident `gateway` Postgres. This migrates the EXISTING objects on a live box into their tables so
history survives the cutover, then leaves the objects in place (the bodies are still the `payload_ref`
/ `input_ref` targets the DB rows point at — only the *index* moved).

  results bucket:
    predictions/<pid>.json            -> predictions row   (ON CONFLICT DO NOTHING)
    labels/<pid>.json                 -> labels row        (write-once PK; a dup is skipped)
    inputs/<modality>/<ts>-<pid>.json -> capture_index row (ON CONFLICT DO NOTHING; body = input_ref)
    policies/<model>.json             -> policies row       (skipped if the DB already has it)
    policies/_pending/<model>.json    -> policies.pending column (only if the policy row exists + empty)
    policies/_status/<model>.json     -> policies.status  column (only if the policy row exists + empty)
    suggestions/<id>.json             -> suggestions row   (skipped if the DB already has the id)
  agent state dir:
    journal.jsonl (one line per transition) -> jobs rows, folded to the final record per job_id

IDEMPOTENT: every write is guarded (ON CONFLICT DO NOTHING, a write-once catch, or an existence
precheck), so a re-run inserts nothing and the counts report shows all-skipped. The DB is the source
of truth after the first pass — an already-migrated row is never clobbered by the (older) object.

Usage (on the box, with the gateway .env sourced):
    python scripts/backfill_store.py [--dsn DSN] [--journal PATH]
Exit status is non-zero iff any row errored (an orphan/ malformed object), so it's CI-checkable.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from platformlib import store as _store  # noqa: E402
from platformlib.topology import STATE_DIR  # noqa: E402

RESULTS_BUCKET = os.getenv("RESULTS_BUCKET", "results")
PRED_PREFIX = "predictions/"
LABEL_PREFIX = "labels/"
INPUT_PREFIX = "inputs/"
POLICY_PREFIX = "policies/"
PENDING_PREFIX = "policies/_pending/"
STATUS_PREFIX = "policies/_status/"
SUGGESTION_PREFIX = "suggestions/"


def _dt(epoch):
    """Epoch float (the object's `ts`) -> a tz-aware datetime for the predictions/labels/capture
    writers (which take a datetime; the policy/suggestion writers take the epoch float directly)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else None


def _model_from_key(key: str, prefix: str) -> str:
    """`<prefix><model>.json` -> `<model>` (the filename is authoritative — a `_status` object body
    carries no model_name)."""
    name = key[len(prefix):]
    return name[:-len(".json")] if name.endswith(".json") else name


def _counts() -> dict:
    return {"scanned": 0, "inserted": 0, "skipped": 0, "errors": 0}


def _warn(table: str, ref, err) -> None:
    print(f"[backfill:WARN] {table} {ref}: {err}", file=sys.stderr)


def _get_json(s3, bucket: str, key: str) -> dict:
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


# -- results-bucket object tables ------------------------------------------------------------------

def _backfill_predictions(s3, conn, store) -> dict:
    c = _counts()
    for key in store.list_keys(s3, RESULTS_BUCKET, PRED_PREFIX):
        c["scanned"] += 1
        try:
            rec = _get_json(s3, RESULTS_BUCKET, key)
            pid = rec["prediction_id"]
            if store.prediction_exists(conn, pid):
                c["skipped"] += 1
                continue
            # `prediction is None` ⇔ streamed / capture-off (unscorable); the object key is the
            # payload_ref the scoring window fetches the captured output from.
            store.log_prediction(conn, pid, rec["model_name"], str(rec["model_version"]),
                                 rec["modality"], _dt(rec.get("ts")),
                                 streamed=(rec.get("prediction") is None), payload_ref=key)
            c["inserted"] += 1
        except Exception as e:  # noqa: BLE001 — one bad object must not abort the migration
            c["errors"] += 1
            _warn("predictions", key, e)
    return c


def _backfill_labels(s3, conn, store) -> dict:
    c = _counts()
    for key in store.list_keys(s3, RESULTS_BUCKET, LABEL_PREFIX):
        c["scanned"] += 1
        try:
            rec = _get_json(s3, RESULTS_BUCKET, key)
            store.attach_label(conn, rec["prediction_id"], rec["label"], _dt(rec.get("ts")))
            c["inserted"] += 1
        except store.LabelExists:
            c["skipped"] += 1                       # write-once PK already holds this label
        except Exception as e:  # noqa: BLE001 — e.g. an orphan label whose prediction wasn't migrated
            c["errors"] += 1
            _warn("labels", key, e)
    return c


def _backfill_capture(s3, conn, store) -> dict:
    c = _counts()
    for key in store.list_keys(s3, RESULTS_BUCKET, INPUT_PREFIX):
        c["scanned"] += 1
        try:
            rec = _get_json(s3, RESULTS_BUCKET, key)
            pid = rec["prediction_id"]
            if store.capture_exists(conn, pid):
                c["skipped"] += 1
                continue
            store.capture_input(conn, pid, rec["modality"], key, _dt(rec.get("ts")))
            c["inserted"] += 1
        except Exception as e:  # noqa: BLE001
            c["errors"] += 1
            _warn("capture", key, e)
    return c


def _backfill_policies(s3, conn, store) -> dict:
    c = _counts()
    for key in store.list_keys(s3, RESULTS_BUCKET, POLICY_PREFIX):
        if key.startswith((PENDING_PREFIX, STATUS_PREFIX)):
            continue                                # sub-prefixes share policies/ — done separately
        c["scanned"] += 1
        try:
            doc = _get_json(s3, RESULTS_BUCKET, key)
            model = doc.get("model_name") or _model_from_key(key, POLICY_PREFIX)
            if store.get_policy(conn, model) is not None:
                c["skipped"] += 1                   # DB is source of truth post-migration — no clobber
                continue
            store.put_policy(conn, model, doc, doc.get("updated_at"),
                             doc.get("updated_by", "operator"))
            c["inserted"] += 1
        except Exception as e:  # noqa: BLE001
            c["errors"] += 1
            _warn("policies", key, e)
    return c


def _backfill_column(s3, conn, store, prefix, getter, setter, table) -> dict:
    """Shared body for the pending/status columns: each folds onto the policy row (UPDATE-only), so it
    migrates only when the policy exists and the column is still empty — an orphan object (no policy)
    or an already-set column is skipped, never clobbered."""
    c = _counts()
    for key in store.list_keys(s3, RESULTS_BUCKET, prefix):
        c["scanned"] += 1
        try:
            body = _get_json(s3, RESULTS_BUCKET, key)
            model = _model_from_key(key, prefix)    # the filename is authoritative (status has no name)
            if store.get_policy(conn, model) is None:
                c["skipped"] += 1                   # orphan — the policy it folds onto is gone
                _warn(table, key, f"no policy row for model {model!r} — skipped")
                continue
            if getter(conn, model) is not None:
                c["skipped"] += 1
                continue
            setter(conn, model, body)
            c["inserted"] += 1
        except Exception as e:  # noqa: BLE001
            c["errors"] += 1
            _warn(table, key, e)
    return c


def _backfill_suggestions(s3, conn, store) -> dict:
    c = _counts()
    for key in store.list_keys(s3, RESULTS_BUCKET, SUGGESTION_PREFIX):
        c["scanned"] += 1
        try:
            rec = _get_json(s3, RESULTS_BUCKET, key)
            if store.get_suggestion(conn, rec["id"]) is not None:
                c["skipped"] += 1
                continue
            store.create_suggestion(conn, rec)      # created_at/resolved_at are epoch floats in the body
            c["inserted"] += 1
        except Exception as e:  # noqa: BLE001
            c["errors"] += 1
            _warn("suggestions", key, e)
    return c


# -- agent journal (JSONL fold) --------------------------------------------------------------------

def _fold_journal(path) -> list:
    """Fold the transition log to the final record per job_id — the exact left-fold the retired
    hostagent.journal._replay did (a submit line carries `record`; later lines set `to`/`reason` and
    add the kind's result fields; control keys never shadow). A torn tail line is dropped whole."""
    jobs = {}
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue                             # crash-torn tail — the prefix fold is intact
            job_id = entry.get("job_id")
            if not job_id:
                continue
            rec = jobs.get(job_id)
            if rec is None:
                rec = dict(entry.get("record") or {"job_id": job_id})
                jobs[job_id] = rec
            if entry.get("to"):
                rec["state"] = entry["to"]
            if entry.get("reason") is not None:
                rec["reason"] = entry["reason"]
            for k, v in entry.items():
                if k not in ("record", "from", "to", "ts", "job_id") and v is not None:
                    rec[k] = v
    return list(jobs.values())


def _backfill_jobs(conn, store, journal_path) -> dict:
    c = _counts()
    for rec in _fold_journal(journal_path):
        c["scanned"] += 1
        try:
            if store.import_job(conn, rec):          # ON CONFLICT DO NOTHING → bool inserted?
                c["inserted"] += 1
            else:
                c["skipped"] += 1
        except Exception as e:  # noqa: BLE001
            c["errors"] += 1
            _warn("jobs", rec.get("job_id"), e)
    return c


def backfill_all(s3, conn, *, store=_store, journal_path=None) -> dict:
    """Migrate every object table in FK-safe order (predictions before labels; policies before their
    pending/status columns). Returns a per-table `{scanned, inserted, skipped, errors}` report."""
    return {
        "predictions": _backfill_predictions(s3, conn, store),
        "labels": _backfill_labels(s3, conn, store),
        "capture": _backfill_capture(s3, conn, store),
        "policies": _backfill_policies(s3, conn, store),
        "pending": _backfill_column(s3, conn, store, PENDING_PREFIX,
                                    store.get_pending, store.set_pending, "pending"),
        "status": _backfill_column(s3, conn, store, STATUS_PREFIX,
                                   store.get_status, store.set_status, "status"),
        "suggestions": _backfill_suggestions(s3, conn, store),
        "jobs": _backfill_jobs(conn, store, journal_path),
    }


def _print_report(report: dict) -> None:
    print(f"{'table':<14}{'scanned':>9}{'inserted':>10}{'skipped':>9}{'errors':>8}")
    print("-" * 50)
    totals = _counts()
    for table, c in report.items():
        print(f"{table:<14}{c['scanned']:>9}{c['inserted']:>10}{c['skipped']:>9}{c['errors']:>8}")
        for k in totals:
            totals[k] += c[k]
    print("-" * 50)
    print(f"{'TOTAL':<14}{totals['scanned']:>9}{totals['inserted']:>10}"
          f"{totals['skipped']:>9}{totals['errors']:>8}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Backfill pre-US4 MinIO objects into the gateway Postgres store (idempotent).")
    ap.add_argument("--dsn", default=None, help="gateway DB DSN (default: store.dsn() from env)")
    ap.add_argument("--journal", default=None,
                    help="legacy journal.jsonl path (default: STATE_DIR/journal.jsonl)")
    args = ap.parse_args(argv)
    journal_path = args.journal or os.path.join(STATE_DIR, "journal.jsonl")

    conn = _store.connect(args.dsn)
    _store.bootstrap(conn)
    s3 = _store.s3_client()
    try:
        report = backfill_all(s3, conn, journal_path=journal_path)
    finally:
        conn.close()
    _print_report(report)
    return 1 if any(c["errors"] for c in report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
