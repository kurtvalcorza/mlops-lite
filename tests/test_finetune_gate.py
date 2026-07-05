"""014 US2 — the training readiness gate (T262 / SC-084).

`finetune_flow` runs `validate_dataset_or_raise` BEFORE the MLflow run / `train_lora`, so a failing
dataset is rejected fast (no GPU acquisition, no wasted MLflow run). This pins the gate function: a
broken dataset raises `DatasetValidationError` (carrying the report); a clean one returns a passing
report. The gate runs against an injected fake store, so no live Garage/GPU is needed.
"""
import importlib.util
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOWS = os.path.join(REPO, "training", "flows")


def _load_finetune():
    if FLOWS not in sys.path:
        sys.path.insert(0, FLOWS)            # so finetune.py's `from lineage import …` resolves
    spec = importlib.util.spec_from_file_location(
        "finetune_ut", os.path.join(FLOWS, "finetune.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ft = _load_finetune()


class FakeS3:
    def __init__(self, raw):
        self.raw = raw

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.raw)}


def _jsonl(rows):
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


def test_gate_rejects_a_failing_dataset_before_training(monkeypatch):
    # missing the response column AND too few rows → a hard-gate failure.
    monkeypatch.setattr(ft, "_s3", lambda: FakeS3(_jsonl([{"instruction": "q"}])))
    try:
        ft.validate_dataset_or_raise("bad", "v1")
    except ft.DatasetValidationError as e:
        assert e.report["passed"] is False
        assert "required_columns" in e.report["gate_failures"] or "min_rows" in e.report["gate_failures"]
    else:
        raise AssertionError("expected DatasetValidationError for a failing dataset")


def test_gate_passes_a_clean_dataset(monkeypatch):
    rows = [{"instruction": f"q{i}", "response": f"a{i}"} for i in range(20)]
    monkeypatch.setattr(ft, "_s3", lambda: FakeS3(_jsonl(rows)))
    report = ft.validate_dataset_or_raise("good", "v1")
    assert report["passed"] is True and report["stats"]["row_count"] == 20


def test_gate_error_carries_the_structured_report(monkeypatch):
    monkeypatch.setattr(ft, "_s3", lambda: FakeS3(b"\n"))  # empty
    try:
        ft.validate_dataset_or_raise("empty", "v1")
    except ft.DatasetValidationError as e:
        assert "rules" in e.report and e.report["stats"]["row_count"] == 0
    else:
        raise AssertionError("expected DatasetValidationError for an empty dataset")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
