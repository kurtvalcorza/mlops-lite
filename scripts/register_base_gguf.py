"""Register the local base GGUFs as first-class registry records (022 T462 — research R2/R7).

The serving resolver (hostagent/serving_llm.py) maps a fine-tune's `base_model` lineage to a
**registered** `kind=full-model` text-generation version and serves `-m <base> --lora <adapter>`.
That only works if the bases in the local zoo (`~/models/gguf/`) exist as registry records — this
script creates them: one `kind=full-model` version per base, `source` pointing at the local GGUF,
tagged so both the name-match and the `base_id`-match resolution paths hit:

  - `task=text-generation` + `serving_engine=llama.cpp` (009 routing metadata),
  - `kind=full-model` + `format=gguf` (the 022 base-vs-adapter discriminator),
  - `base_id=<HF id>` — the raw base string the trainer stamps into an adapter's `base_model`
    (e.g. `Qwen/Qwen2.5-0.5B-Instruct`), so lineage written before 022 resolves too.

Idempotent: a base whose (name, source) is already registered is skipped — re-runs register
nothing new. A base GGUF absent from the zoo is skipped with a note (a 16 GB-budget box may carry
only one base; Principle I/III — nothing is downloaded here, ever).

Usage: python scripts/register_base_gguf.py   (host side; MLFLOW_TRACKING_URI/GGUF_DIR to override)
"""
import os
import sys

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
GGUF_DIR = os.path.expanduser(os.getenv("GGUF_DIR", "~/models/gguf"))

# The local zoo (research R7 — bounded, operator-curated). `name` is the registered-model name the
# platform serves under (the 7B matches SERVING_MODEL); `base_id` is the HF id adapters record as
# `base_model`; `file` follows the zoo naming (the 0.5B f16 comes from scoring's resolve_base_gguf).
BASES = [
    {"name": "qwen2.5-7b-instruct-q4_k_m",
     "base_id": "Qwen/Qwen2.5-7B-Instruct",
     "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf"},
    {"name": "qwen2.5-0.5b-instruct",
     "base_id": "Qwen/Qwen2.5-0.5B-Instruct",
     "file": "Qwen_Qwen2.5-0.5B-Instruct-f16.gguf"},
]


def register_bases(client, bases=None, gguf_dir=None, log=print) -> dict:
    """Register each present base GGUF once. Returns {"registered": [...], "skipped": [...]} —
    separated from main() so the idempotency is unit-testable against a fake client."""
    from mlflow.exceptions import MlflowException

    gguf_dir = gguf_dir or GGUF_DIR
    report = {"registered": [], "skipped": []}
    for base in bases or BASES:
        path = os.path.join(gguf_dir, base["file"])
        if not os.path.isfile(path):
            report["skipped"].append({"name": base["name"], "reason": f"no GGUF at {path}"})
            log(f"~ {base['name']}: no GGUF at {path} — skipped (nothing is downloaded)")
            continue
        try:
            existing = client.search_model_versions(f"name='{base['name']}'")
        except MlflowException:
            existing = []
        if any(mv.source == path for mv in existing):
            report["skipped"].append({"name": base["name"], "reason": "already registered"})
            log(f"= {base['name']}: already registered from {path}")
            continue
        try:
            client.create_registered_model(base["name"])
        except MlflowException:
            pass  # exists — adding a version is fine
        mv = client.create_model_version(
            name=base["name"], source=path, run_id=None,
            tags={"kind": "full-model", "format": "gguf", "task": "text-generation",
                  "serving_engine": "llama.cpp", "base_id": base["base_id"]})
        report["registered"].append({"name": base["name"], "version": str(mv.version),
                                     "source": path})
        log(f"+ {base['name']} v{mv.version} <- {path}")
    return report


def main() -> int:
    from mlflow.tracking import MlflowClient

    report = register_bases(MlflowClient(tracking_uri=MLFLOW_URI))
    print(f"done: {len(report['registered'])} registered, {len(report['skipped'])} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
