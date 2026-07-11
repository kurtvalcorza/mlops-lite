"""Register the local base GGUFs as first-class registry records (022 T462 — research R2/R7).

The serving resolver (hostagent/serving_llm.py) maps a fine-tune's `base_model` lineage to a
**registered** `kind=full-model` text-generation version and serves `-m <base> --lora <adapter>`.
That only works if the bases in the local zoo (`~/models/gguf/`) exist as registry records — this
script creates them: one `kind=full-model` version per base, tagged so both the name-match and the
`base_id`-match resolution paths hit:

  - `task=text-generation` + `serving_engine=llama.cpp` (009 routing metadata),
  - `kind=full-model` + `format=gguf` (the 022 base-vs-adapter discriminator),
  - `base_id=<HF id>` — the raw base string the trainer stamps into an adapter's `base_model`
    (e.g. `Qwen/Qwen2.5-0.5B-Instruct`), so lineage written before 022 resolves too.

**The version `source` is an `s3://` object in the platform store (Garage), NOT a local path**
(022 on-HW finding): MLflow 3.x REJECTS a bare local-path model-version source
(`Invalid model version source … the run_id request parameter has to be specified`), and the agent
materializes an `s3://` source from the same Garage the adapters already live in (research R7). So
this uploads each present base GGUF to the store once, then registers the `s3://` source.

Idempotent: a base whose (name, `s3://` source) is already registered is skipped, and the upload is
skipped when the object is already present at the same size — re-runs register/upload nothing new. A
base GGUF absent from the zoo is skipped with a note (a 16 GB-budget box may carry only one base;
Principle I/III — nothing is downloaded here, ever).

Usage: python scripts/register_base_gguf.py   (host side; MLFLOW_TRACKING_URI/GGUF_DIR to override;
the store client reads AWS_*/MLFLOW_S3_ENDPOINT_URL from the env, same as the rest of the platform)
"""
import os
import sys

# Make `platformlib` importable when this is run as a standalone script (the s3 upload uses the
# shared store client) — the repo root isn't on sys.path otherwise (scripts/ is one level down).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
GGUF_DIR = os.path.expanduser(os.getenv("GGUF_DIR", "~/models/gguf"))
#: The object-store bucket + prefix the base GGUFs are uploaded under (the models bucket the adapters
#: and MLflow artifacts share). `source` becomes `s3://<bucket>/<prefix>/<name>.gguf`.
MODELS_BUCKET = os.getenv("MODELS_BUCKET", "models")
BASE_PREFIX = os.getenv("BASE_ZOO_PREFIX", "base-zoo")


def _garage_upload(path: str, bucket: str, key: str, log=print) -> None:
    """Upload a local GGUF to the platform object store (Garage) once — idempotent by (bucket, key)
    + size, so a re-run re-uploads nothing. Uses the shared `platformlib.store` client (env creds +
    `MLFLOW_S3_ENDPOINT_URL`), the same access path the agent uses to materialize the artifact."""
    from platformlib.store import s3_client

    s3 = s3_client()
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        if head.get("ContentLength") == os.path.getsize(path):
            log(f"= object already in the store: s3://{bucket}/{key} (skip upload)")
            return
    except Exception:  # noqa: BLE001 — absent (or head not permitted) → upload
        pass
    s3.upload_file(path, bucket, key)
    log(f"^ uploaded {path} -> s3://{bucket}/{key}")

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


def register_bases(client, bases=None, gguf_dir=None, log=print, *, upload=None,
                   bucket=None, prefix=None) -> dict:
    """Register each present base GGUF once (uploading it to the store first). Returns
    {"registered": [...], "skipped": [...]} — separated from main() so the upload + idempotency are
    unit-testable against a fake client + injected `upload` (no live Garage/MLflow)."""
    from mlflow.exceptions import MlflowException

    gguf_dir = gguf_dir or GGUF_DIR
    bucket = bucket or MODELS_BUCKET
    prefix = prefix if prefix is not None else BASE_PREFIX
    upload = upload or _garage_upload
    report = {"registered": [], "skipped": []}
    for base in bases or BASES:
        path = os.path.join(gguf_dir, base["file"])
        if not os.path.isfile(path):
            report["skipped"].append({"name": base["name"], "reason": f"no GGUF at {path}"})
            log(f"~ {base['name']}: no GGUF at {path} — skipped (nothing is downloaded)")
            continue
        key = f"{prefix}/{base['name']}.gguf"
        src = f"s3://{bucket}/{key}"
        try:
            existing = client.search_model_versions(f"name='{base['name']}'")
        except MlflowException:
            existing = []
        if any(mv.source == src for mv in existing):
            report["skipped"].append({"name": base["name"], "reason": "already registered"})
            log(f"= {base['name']}: already registered from {src}")
            continue
        # Upload the GGUF to the store (idempotent) BEFORE registering — MLflow 3.x rejects a bare
        # local-path source, and the agent materializes this s3:// object from Garage (022 on-HW).
        upload(path, bucket, key, log)
        try:
            client.create_registered_model(base["name"])
        except MlflowException:
            pass  # exists — adding a version is fine
        mv = client.create_model_version(
            name=base["name"], source=src, run_id=None,
            tags={"kind": "full-model", "format": "gguf", "task": "text-generation",
                  "serving_engine": "llama.cpp", "base_id": base["base_id"]})
        report["registered"].append({"name": base["name"], "version": str(mv.version),
                                     "source": src})
        log(f"+ {base['name']} v{mv.version} <- {src}")
    return report


def main() -> int:
    from mlflow.tracking import MlflowClient

    report = register_bases(MlflowClient(tracking_uri=MLFLOW_URI))
    print(f"done: {len(report['registered'])} registered, {len(report['skipped'])} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
