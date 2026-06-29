#!/usr/bin/env python3
"""One-shot evaluation harness entry (011 US1, T207, FR-100).

Score a registered model version on its modality's held-out benchmark and log the primary metric to
MLflow against the version (tags + run metric) with benchmark provenance — handy for batch eval or
seeding the incumbent's metric before the first gated promotion. Also exposes the offline
champion-challenger comparison (US3).

Runs gateway-side code directly, so it needs the gateway's env (MLFLOW_TRACKING_URI, SERVING_URL /
BENTO_URL) and the relevant serving daemon up — it drives the live serving path (one model in VRAM).

Usage:
  python scripts/eval_model.py evaluate <name> <version> [--benchmark PATH] [--metric NAME]
  python scripts/eval_model.py compare  <name> <challenger_version> [--benchmark PATH] [--metric NAME]
"""
import argparse
import json
import os
import sys

# Import the gateway harness whether run from the repo root or elsewhere.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway"))
from app import evaluation  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Offline evaluation harness / champion-challenger (011).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("evaluate", help="score one version + log its primary metric to MLflow")
    pe.add_argument("name")
    pe.add_argument("version")
    pe.add_argument("--benchmark", default=None, help="path under benchmarks/ (default: modality fixture)")
    pe.add_argument("--metric", default=None, help="override the modality's default primary metric")

    pc = sub.add_parser("compare", help="offline champion (@serving) vs challenger on the held-out set")
    pc.add_argument("name")
    pc.add_argument("challenger")
    pc.add_argument("--benchmark", default=None)
    pc.add_argument("--metric", default=None)

    a = p.parse_args()
    try:
        if a.cmd == "evaluate":
            out = evaluation.evaluate(a.name, a.version, a.benchmark, metric_name=a.metric)
        else:
            out = evaluation.compare(a.name, a.challenger, a.benchmark, metric_name=a.metric)
    except evaluation.EvalError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
