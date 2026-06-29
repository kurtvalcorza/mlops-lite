#!/usr/bin/env python3
"""Retag the serving LLM with 009 routing metadata (T158, US1 / FR-074, FR-086).

The serving LLM was registered before 009 with no `task`/`serving_engine` version tags. This adds
them **in place** (via `set_model_version_tag`, no re-registration) so the gateway resolves the
text-generation serving target off registry metadata (FR-075) and the Infer tab renders its panel
from the registry (FR-077). Idempotent — re-running just re-asserts the tags.

Targets the version currently promoted to `@serving` (the one /infer resolves); if nothing is
promoted yet, it tags+promotes the latest version so a panel still resolves. A full fresh-backend
bring-up should instead use `scripts/reseed_registry.sh`, which now registers the LLM already tagged.

Run in WSL with the training venv:  ~/mlops-train/bin/python scripts/retag_serving_llm.py
"""
import os
import sys

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
NAME = os.getenv("SERVING_MODEL", "qwen2.5-7b-instruct-q4_k_m")
SERVING_ALIAS = "serving"


def main() -> int:
    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.get_registered_model(NAME)
    except MlflowException:
        print(f"[FAIL] no registered model '{NAME}' — register it first "
              f"(scripts/reseed_registry.sh) before retagging", file=sys.stderr)
        return 1

    # Prefer the @serving version (the one /infer resolves); else the latest, which we then promote.
    version = None
    try:
        version = str(c.get_model_version_by_alias(NAME, SERVING_ALIAS).version)
    except MlflowException:
        versions = sorted(c.search_model_versions(f"name='{NAME}'"),
                          key=lambda mv: int(mv.version), reverse=True)
        if not versions:
            print(f"[FAIL] '{NAME}' has no versions to tag", file=sys.stderr)
            return 1
        version = str(versions[0].version)

    for k, v in (("task", "text-generation"), ("serving_engine", "llama.cpp")):
        c.set_model_version_tag(NAME, version, k, v)
    c.set_registered_model_alias(NAME, SERVING_ALIAS, version)  # idempotent; ensures a panel resolves
    print(f"retagged {NAME} v{version}: task=text-generation, serving_engine=llama.cpp "
          f"(promoted @serving)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
