"""Backfill task descriptors onto legacy text-generation versions (022 T477 — FR-267).

Fine-tunes registered before 022 carry `kind=lora-adapter`/`format=gguf` but NO `task` tag, so the
console showed them as "no renderer" placeholders and the serving surfaces couldn't route them
(the ops-bot-v1/v2 gap observed live). This one-time backfill stamps what T476 now writes at
registration:

    task=text-generation      (identified by kind=lora-adapter OR format=gguf — GGUF is
    serving_engine=llama.cpp   llama.cpp's format, so the inference is unambiguous)

Idempotent + NON-CLOBBER: an existing `task` or `serving_engine` tag is never overwritten — a
re-run changes nothing (the report's `skipped` count proves it). Read + tag only; no alias moves,
no artifact writes.

Usage: python scripts/backfill_llm_task_tags.py   (host side; MLFLOW_TRACKING_URI to override)
"""
import os
import sys

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")


def backfill(client, log=print) -> dict:
    """Stamp missing task/serving_engine tags on every legacy LLM-shaped version. Returns
    {"tagged": [...], "skipped": [...]} — separated from main() so idempotency and the non-clobber
    rule are unit-testable against a fake client."""
    from platformlib import llmresolve

    # Two searches because MLflow filter strings have no OR: adapters by kind, bases/full GGUFs by
    # format. De-duped by (name, version).
    seen, candidates = set(), []
    for query in (f"tags.{llmresolve.KIND_TAG}='{llmresolve.KIND_ADAPTER}'",
                  f"tags.{llmresolve.FORMAT_TAG}='gguf'"):
        for mv in client.search_model_versions(query):
            key = (mv.name, str(mv.version))
            if key not in seen:
                seen.add(key)
                candidates.append(mv)

    report = {"tagged": [], "skipped": []}
    for mv in candidates:
        tags = dict(mv.tags or {})
        if llmresolve.task_from_kind(tags) != llmresolve.TEXT_GENERATION:
            report["skipped"].append({"name": mv.name, "version": str(mv.version),
                                      "reason": "not an LLM artifact shape"})
            continue
        missing = {}
        if not tags.get(llmresolve.TASK_TAG):  # non-clobber: only ever fill a missing tag
            missing[llmresolve.TASK_TAG] = llmresolve.TEXT_GENERATION
        if not tags.get(llmresolve.ENGINE_TAG):
            missing[llmresolve.ENGINE_TAG] = llmresolve.LLAMA_ENGINE
        if not missing:
            report["skipped"].append({"name": mv.name, "version": str(mv.version),
                                      "reason": "already tagged"})
            log(f"= {mv.name} v{mv.version}: already tagged")
            continue
        for key, value in missing.items():
            client.set_model_version_tag(mv.name, str(mv.version), key, value)
        report["tagged"].append({"name": mv.name, "version": str(mv.version),
                                 "added": sorted(missing)})
        log(f"+ {mv.name} v{mv.version}: {', '.join(f'{k}={v}' for k, v in missing.items())}")
    return report


def main() -> int:
    from mlflow.tracking import MlflowClient

    report = backfill(MlflowClient(tracking_uri=MLFLOW_URI))
    print(f"done: {len(report['tagged'])} tagged, {len(report['skipped'])} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
