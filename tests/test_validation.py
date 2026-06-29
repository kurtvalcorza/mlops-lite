"""014 US2 — hand-rolled data validation (T261 / SC-083).

Pure, offline coverage of the rule set + structured report: a clean dataset passes; deliberately-broken
datasets (empty / missing required columns / over-null / under-min-rows / imbalanced labels) fail with
each offending rule named value-vs-threshold. Gate vs warn dispositions are respected (only gate-rule
failures flip `passed`).
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "validation_ut", os.path.join(REPO, "gateway", "app", "validation.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load()


def _clean(n=20):
    return [{"instruction": f"q{i}", "response": f"a{i}"} for i in range(n)]


# --- happy path -----------------------------------------------------------------------------------

def test_clean_dataset_passes_with_stats():
    r = v.run_rules(_clean(20))
    assert r["passed"] is True
    assert r["stats"]["row_count"] == 20
    assert set(r["stats"]["columns"]) == {"instruction", "response"}
    assert r["gate_failures"] == [] and r["warnings"] == []
    by = {rule["name"]: rule for rule in r["rules"]}
    assert by["required_columns"]["passed"] and by["min_rows"]["passed"]


def test_prompt_output_aliases_satisfy_required_columns():
    # any-of groups: prompt/output are accepted in place of instruction/response.
    r = v.run_rules([{"prompt": f"q{i}", "output": f"a{i}"} for i in range(15)])
    assert r["passed"] is True


# --- gate failures (flip passed) ------------------------------------------------------------------

def test_empty_dataset_fails_gate():
    r = v.run_rules([])
    assert r["passed"] is False
    assert "min_rows" in r["gate_failures"] and "required_columns" in r["gate_failures"]


def test_missing_required_column_fails_gate():
    r = v.run_rules([{"instruction": f"q{i}"} for i in range(20)])  # no response/output/completion
    assert r["passed"] is False
    req = next(rule for rule in r["rules"] if rule["name"] == "required_columns")
    assert req["passed"] is False and "response" in req["detail"]


def test_under_min_rows_fails_gate_with_value_vs_threshold():
    r = v.run_rules(_clean(3), min_rows=10)
    assert r["passed"] is False
    mr = next(rule for rule in r["rules"] if rule["name"] == "min_rows")
    assert mr["value"] == 3 and mr["threshold"] == 10 and "3 rows < 10" in mr["detail"]


# --- warn failures (do NOT flip passed) -----------------------------------------------------------

def test_high_null_rate_warns_but_does_not_block():
    rows = [{"instruction": f"q{i}", "response": ("" if i < 10 else f"a{i}")} for i in range(20)]
    r = v.run_rules(rows, max_null_rate=0.2)
    assert r["passed"] is True                 # null_rate is a WARN rule — doesn't gate
    assert "null_rate" in r["warnings"]
    nr = next(rule for rule in r["rules"] if rule["name"] == "null_rate")
    assert nr["value"] == 0.5 and nr["threshold"] == 0.2


def test_value_range_warns():
    rows = [{"instruction": f"q{i}", "response": f"a{i}", "score": str(i)} for i in range(20)]
    r = v.run_rules(rows, ranges={"score": (0, 5)})
    assert r["passed"] is True and "value_range" in r["warnings"]


def test_label_balance_warns_on_skew():
    rows = ([{"instruction": "q", "response": "a", "label": "x"} for _ in range(19)]
            + [{"instruction": "q", "response": "a", "label": "y"}])  # 1/20 = 0.05 smallest
    r = v.run_rules(rows, label_column="label", min_label_fraction=0.1)
    assert r["passed"] is True and "label_balance" in r["warnings"]
    lb = next(rule for rule in r["rules"] if rule["name"] == "label_balance")
    assert lb["value"] == 0.05


def test_disposition_is_configurable_null_rate_as_gate():
    rows = [{"instruction": f"q{i}", "response": ("" if i < 10 else f"a{i}")} for i in range(20)]
    r = v.run_rules(rows, max_null_rate=0.2, dispositions={"null_rate": v.GATE})
    assert r["passed"] is False and "null_rate" in r["gate_failures"]  # now it gates


# --- parsing --------------------------------------------------------------------------------------

def test_parse_jsonl_and_csv():
    jsonl = b'{"instruction":"q","response":"a"}\n{"instruction":"q2","response":"a2"}\n'
    assert len(v.parse_rows(jsonl, "jsonl")) == 2
    csv_bytes = b"instruction,response\nq,a\nq2,a2\n"
    rows = v.parse_rows(csv_bytes, "csv")
    assert len(rows) == 2 and rows[0]["instruction"] == "q"


def test_parse_skips_malformed_jsonl_lines():
    raw = b'{"instruction":"q","response":"a"}\nNOT JSON\n{"instruction":"q2","response":"a2"}\n'
    assert len(v.parse_rows(raw, "jsonl")) == 2  # the bad line is dropped, not fatal


def test_parse_sniffs_csv_without_explicit_fmt():
    # The training gate + batch loader call parse_rows() with NO fmt hint — a CSV must still be
    # detected by its comma-delimited header, not silently parsed as JSONL (→ 0 rows → false gate fail).
    csv_bytes = b"instruction,response\nq,a\nq2,a2\n"
    rows = v.parse_rows(csv_bytes)
    assert len(rows) == 2 and rows[0]["instruction"] == "q"


def test_parse_sniffs_jsonl_without_explicit_fmt():
    jsonl = b'{"instruction":"q","response":"a"}\n{"instruction":"q2","response":"a2"}\n'
    assert len(v.parse_rows(jsonl)) == 2  # a leading `{` is JSON-ish, not CSV


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
