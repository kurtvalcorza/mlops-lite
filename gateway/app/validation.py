"""Lightweight, hand-rolled data validation (014, US2) — readiness gates for datasets.

Training consumes a dataset version with no readiness check, so a malformed/empty/schema-drifted dataset
fails deep inside the LoRA loop instead of fast at the edge. This module computes a structured
**ValidationReport** over a dataset version's bytes — required columns, row count, per-column null rate,
numeric value ranges, label balance — and is consumed by **two** call sites with one implementation: the
gateway `POST /datasets/{name}/{version}/validate` route (advisory) and the training gate in
`finetune_flow` (hard gate before `train_lora`).

**Hand-rolled on purpose (Principle III).** Like `datasets.py` (DVC → content-addressing) and
`monitoring.py` (Evidently → PSI), this delivers the same guarantee with a fraction of the weight — a few
pure-Python rules over parsed rows, **no Great Expectations / no pandera / no new dependency**.

Each rule carries a **gate-vs-warn disposition** (defaults: required-columns + min-row-count **gate**;
null-rate, value-range, label-balance **warn**), and every rule + threshold + disposition is
**configurable** (the defaults are sensible starting points, not immutable). `report["passed"]` reflects
only the **gate** rules — a warn-rule failure is flagged in the report but does not block.

The pure core (`run_rules`) takes already-parsed rows so it unit-tests with no S3 and no GPU; the I/O
wrapper (`validate_dataset`) loads the bytes from the existing dataset registry on top.
"""
import csv
import io
import json
import os

GATE, WARN = "gate", "warn"

# Default rule set + thresholds (all operator-overridable per call). `required_columns` entries are
# **any-of groups**: a group is satisfied when at least one of its members is present in every row — so
# instruction-tuning datasets that use `prompt`/`instruction` (or `response`/`output`/`completion`)
# interchangeably still pass.
DEFAULT_REQUIRED = [["instruction", "prompt"], ["response", "output", "completion"]]
DEFAULT_MIN_ROWS = int(os.getenv("VALIDATION_MIN_ROWS", "10"))
DEFAULT_MAX_NULL_RATE = float(os.getenv("VALIDATION_MAX_NULL_RATE", "0.2"))
DEFAULT_MIN_LABEL_FRACTION = float(os.getenv("VALIDATION_MIN_LABEL_FRACTION", "0.05"))


class ValidationError(Exception):
    """Validation could not run (dataset unreadable / unparseable) — distinct from a dataset that runs
    and simply fails its rules (that is a `passed=false` report, not an exception)."""


# --- dataset parsing (jsonl / csv) ----------------------------------------------------------------

def parse_rows(raw: bytes, fmt=None) -> list:
    """Parse dataset bytes into a list of dict rows. JSONL (one object per line) and CSV are supported;
    `fmt` hints the format, else it's sniffed (a leading `{`/`[` ⇒ JSON-ish)."""
    text = raw.decode("utf-8", errors="replace")
    fmt = (fmt or "").lower()
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    if fmt in ("csv",) or (not fmt and "," in first_line and not text.lstrip().startswith(("{", "["))):
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # a malformed line isn't a row; the schema/row-count rules will catch a bad file
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


# --- the rules (pure) -----------------------------------------------------------------------------

def _present_columns(rows: list) -> set:
    """Columns present in EVERY row (intersection of keys) — a column only 'exists' if all rows carry it."""
    if not rows:
        return set()
    cols = set(rows[0].keys())
    for r in rows[1:]:
        cols &= set(r.keys())
    return cols


def _is_null(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _rule(name, passed, disposition, value, threshold, detail=""):
    return {"name": name, "passed": bool(passed), "disposition": disposition,
            "value": value, "threshold": threshold, "detail": detail}


def run_rules(rows: list, *, required_columns=None, min_rows=DEFAULT_MIN_ROWS,
              max_null_rate=DEFAULT_MAX_NULL_RATE, ranges=None, label_column=None,
              min_label_fraction=DEFAULT_MIN_LABEL_FRACTION, dispositions=None) -> dict:
    """Run the validation rule set over parsed `rows` and return a structured ValidationReport.

    `report["passed"]` is True iff every **gate**-disposition rule passed (warn-rule failures are recorded
    but do not block). Each rule reports `{name, passed, disposition, value, threshold, detail}`.
    """
    required_columns = DEFAULT_REQUIRED if required_columns is None else required_columns
    ranges = ranges or {}
    disp = {"required_columns": GATE, "min_rows": GATE,
            "null_rate": WARN, "value_range": WARN, "label_balance": WARN}
    disp.update(dispositions or {})

    present = _present_columns(rows)
    rules = []

    # required columns / schema (any-of groups)
    missing = []
    for group in required_columns:
        members = [group] if isinstance(group, str) else list(group)
        if not any(m in present for m in members):
            missing.append("|".join(members))
    rules.append(_rule("required_columns", not missing, disp["required_columns"],
                       value=sorted(present), threshold=required_columns,
                       detail=("missing: " + ", ".join(missing)) if missing else ""))

    # minimum row count
    n = len(rows)
    rules.append(_rule("min_rows", n >= min_rows, disp["min_rows"], value=n, threshold=min_rows,
                       detail="" if n >= min_rows else f"{n} rows < {min_rows} minimum"))

    # per-column null rate (the worst column vs the threshold)
    null_rates = {}
    for c in sorted(present):
        nulls = sum(1 for r in rows if _is_null(r.get(c)))
        null_rates[c] = round(nulls / n, 4) if n else 0.0
    worst_col, worst_rate = (max(null_rates.items(), key=lambda kv: kv[1]) if null_rates else (None, 0.0))
    rules.append(_rule("null_rate", worst_rate <= max_null_rate, disp["null_rate"],
                       value=worst_rate, threshold=max_null_rate,
                       detail=f"column {worst_col!r} null rate {worst_rate}" if worst_col else ""))

    # numeric value ranges (any configured column out of [lo, hi] fails)
    range_violations = []
    for col, (lo, hi) in ranges.items():
        for r in rows:
            v = r.get(col)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if (lo is not None and fv < lo) or (hi is not None and fv > hi):
                range_violations.append(f"{col}={fv} not in [{lo},{hi}]")
                break
    rules.append(_rule("value_range", not range_violations, disp["value_range"],
                       value=range_violations[:5], threshold=ranges,
                       detail="; ".join(range_violations[:5])))

    # label balance (smallest class fraction vs the floor)
    label_balance = {}
    lb_passed, lb_value = True, None
    if label_column:
        for r in rows:
            lab = r.get(label_column)
            if lab is not None:
                label_balance[str(lab)] = label_balance.get(str(lab), 0) + 1
        total = sum(label_balance.values())
        if total:
            lb_value = round(min(label_balance.values()) / total, 4)
            lb_passed = lb_value >= min_label_fraction
    rules.append(_rule("label_balance", lb_passed, disp["label_balance"],
                       value=lb_value, threshold=min_label_fraction,
                       detail="" if lb_passed else f"smallest class fraction {lb_value} < {min_label_fraction}"))

    passed = all(r["passed"] for r in rules if r["disposition"] == GATE)
    return {
        "passed": passed,
        "rules": rules,
        "stats": {"row_count": n, "columns": sorted(present), "null_rates": null_rates,
                  "label_balance": label_balance or None},
        "gate_failures": [r["name"] for r in rules if r["disposition"] == GATE and not r["passed"]],
        "warnings": [r["name"] for r in rules if r["disposition"] == WARN and not r["passed"]],
    }


# --- I/O wrapper: load a dataset version's bytes from the registry, then run the rules -------------

def validate_dataset(name: str, version: str, *, fmt=None, **ruleset) -> dict:
    """Load dataset `name@version` from the content-addressed registry (MinIO) and validate it. The
    format is read from the dataset manifest when not given. Raises ValidationError if the bytes can't
    be loaded/parsed; a dataset that loads but fails its rules returns a `passed=false` report."""
    from .datasets import BUCKET, _s3

    s3 = _s3()
    try:
        raw = s3.get_object(Bucket=BUCKET, Key=f"{name}/{version}/data")["Body"].read()
    except Exception as e:
        raise ValidationError(f"cannot load dataset {name}@{version}: {e}") from e
    if fmt is None:
        try:
            manifest = json.loads(
                s3.get_object(Bucket=BUCKET, Key=f"{name}/{version}/manifest.json")["Body"].read())
            fmt = manifest.get("format")
        except Exception:
            fmt = None
    rows = parse_rows(raw, fmt)
    report = run_rules(rows, **ruleset)
    report["dataset"] = f"{name}@{version}"
    return report
