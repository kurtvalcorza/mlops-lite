"""LoRA fine-tune flow (T029/T030, US4) — closes the train -> register -> serve loop.

Runs **natively in WSL** on the single GPU (hybrid GPU, constitution v1.2.0), structured with
Prefect `@flow`/`@task`. Prefect runs *ephemerally* here — no always-on Prefect server (Principle
III, Lightweight Footprint); the decorators give run structure + retries and make the flow ready
for the US5 drift->retrain trigger. If Prefect isn't importable, the decorators degrade to no-ops
so the flow still runs.

Pipeline: pull a pinned dataset version from MinIO -> PEFT/LoRA fine-tune a small base model ->
log params/metrics to MLflow -> convert the adapter to GGUF (llama.cpp) -> upload to MinIO ->
register a new MLflow model version tagged with the run + dataset version (feeds US2).

A small base (default Qwen2.5-0.5B-Instruct) keeps the demo fast and within the VRAM budget
without 4-bit/bitsandbytes (which lacks reliable Blackwell sm_120 kernels). The loop is identical
at any size.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time

# --- Prefect (optional, ephemeral) ------------------------------------------------------------
try:
    from prefect import flow, task
    from prefect.logging import get_run_logger

    def _log(msg):
        try:
            get_run_logger().info(msg)
        except Exception:
            print(msg, flush=True)
except Exception:  # Prefect absent → no-op decorators, plain prints
    def task(fn=None, **_):
        return fn if fn else (lambda f: f)

    def flow(fn=None, **_):
        return fn if fn else (lambda f: f)

    def _log(msg):
        print(msg, flush=True)


# --- Config (env-overridable) -----------------------------------------------------------------
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000")
DATASETS_BUCKET = os.getenv("DATASETS_BUCKET", "datasets")
MODELS_BUCKET = os.getenv("MODELS_BUCKET", "models")
LLAMA_DIR = os.getenv("LLAMA_DIR", os.path.expanduser("~/llama.cpp"))
DEFAULT_BASE = os.getenv("BASE_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")


def _s3():
    import boto3
    from botocore.client import Config
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],  # no hardcoded default (FR-017)
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )


@task
def fetch_dataset(name: str, version: str) -> list:
    """Pull the pinned dataset version from MinIO and parse JSONL instruction/response pairs."""
    raw = _s3().get_object(Bucket=DATASETS_BUCKET, Key=f"{name}/{version}/data")["Body"].read()
    rows = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        instr = obj.get("instruction") or obj.get("prompt") or ""
        resp = obj.get("response") or obj.get("output") or obj.get("completion") or ""
        if instr and resp:
            rows.append({"instruction": instr, "response": resp})
    if not rows:
        raise ValueError(f"dataset {name}@{version} has no usable instruction/response rows")
    _log(f"fetched {len(rows)} training rows from {name}@{version}")
    return rows


@task
def train_lora(base_model: str, rows: list, out_dir: str, steps: int, lora_r: int, seed: int) -> dict:
    """PEFT/LoRA fine-tune on the single GPU (or CPU fallback). Returns metrics + the adapter dir."""
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              DataCollatorForLanguageModeling, Trainer, TrainingArguments)

    torch.manual_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    _log(f"training on {device} ({torch.cuda.get_device_name(0) if device == 'cuda' else 'cpu'})")

    tok = AutoTokenizer.from_pretrained(base_model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(base_model, torch_dtype=dtype).to(device)
    model = get_peft_model(model, LoraConfig(
        r=lora_r, lora_alpha=lora_r * 2, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    ))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    _log(f"LoRA r={lora_r}, trainable params={trainable:,}")

    def _fmt(ex):
        msgs = [{"role": "user", "content": ex["instruction"]},
                {"role": "assistant", "content": ex["response"]}]
        text = tok.apply_chat_template(msgs, tokenize=False)
        out = tok(text, truncation=True, max_length=512, padding="max_length")
        return out

    ds = Dataset.from_list(rows).map(_fmt, remove_columns=["instruction", "response"])
    args = TrainingArguments(
        output_dir=out_dir, max_steps=steps, per_device_train_batch_size=1,
        gradient_accumulation_steps=1, learning_rate=2e-4, logging_steps=1,
        save_strategy="no", report_to=[], seed=seed, fp16=False, bf16=(device == "cuda"),
    )
    trainer = Trainer(
        model=model, args=args, train_dataset=ds,
        data_collator=DataCollatorForLanguageModeling(tok, mlm=False),
    )
    result = trainer.train()
    adapter_dir = os.path.join(out_dir, "adapter")
    model.save_pretrained(adapter_dir)
    tok.save_pretrained(adapter_dir)

    metrics = {
        "train_loss": float(result.training_loss),
        "steps": int(steps),
        "trainable_params": int(trainable),
        "device": device,
    }
    # Free the GPU promptly (Principle II): nothing should stay resident after training.
    del trainer, model
    if device == "cuda":
        torch.cuda.empty_cache()
    _log(f"training done: loss={metrics['train_loss']:.4f}")
    return {"adapter_dir": adapter_dir, "metrics": metrics}


@task
def convert_to_gguf(adapter_dir: str, base_model: str, out_path: str) -> str:
    """Convert the PEFT LoRA adapter to a GGUF adapter llama-server can load via --lora."""
    from huggingface_hub import snapshot_download

    script = os.path.join(LLAMA_DIR, "convert_lora_to_gguf.py")
    if not os.path.exists(script):
        raise FileNotFoundError(f"llama.cpp LoRA converter not found at {script}")
    # The converter's --base expects a *local* model dir, not an HF repo id. Resolve the cached
    # snapshot (already downloaded during training, so this is a no-op lookup).
    base_path = base_model if os.path.isdir(base_model) else snapshot_download(base_model)
    cmd = [sys.executable, script, adapter_dir, "--base", base_path, "--outfile", out_path]
    _log("converting adapter -> GGUF: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"GGUF conversion failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
    _log(f"GGUF adapter written: {out_path} ({os.path.getsize(out_path)} bytes)")
    return out_path


@task
def register_version(output_name: str, gguf_path: str, run_id: str,
                     base_model: str, dataset_name: str, dataset_version: str) -> dict:
    """Upload the GGUF adapter to MinIO and register a new MLflow model version (feeds US2)."""
    from mlflow.exceptions import MlflowException
    from mlflow.tracking import MlflowClient

    key = f"{output_name}/{run_id}/adapter.gguf"
    with open(gguf_path, "rb") as f:
        _s3().put_object(Bucket=MODELS_BUCKET, Key=key, Body=f.read())
    source = f"s3://{MODELS_BUCKET}/{key}"

    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.create_registered_model(output_name)
    except MlflowException:
        pass
    mv = c.create_model_version(
        name=output_name, source=source, run_id=run_id,
        tags={
            "kind": "lora-adapter", "base_model": base_model,
            "dataset_name": dataset_name, "dataset_version": dataset_version,
            "format": "gguf",
        },
    )
    _log(f"registered {output_name} v{mv.version} <- run {run_id} on {dataset_name}@{dataset_version}")
    return {"name": output_name, "version": str(mv.version), "source": source}


@flow(name="lora-finetune")
def finetune_flow(dataset_name: str, dataset_version: str, output_name: str,
                  base_model: str = DEFAULT_BASE, steps: int = 10, lora_r: int = 8,
                  seed: int = 0) -> dict:
    """End-to-end fine-tune: dataset version -> tracked LoRA run -> registered, servable version."""
    import mlflow

    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("lora-finetune")
    with mlflow.start_run() as run:
        run_id = run.info.run_id
        params = dict(base_model=base_model, dataset_name=dataset_name,
                      dataset_version=dataset_version, output_name=output_name,
                      steps=steps, lora_r=lora_r, seed=seed)
        mlflow.log_params(params)  # full config recorded → reproducible (FR-012)

        rows = fetch_dataset(dataset_name, dataset_version)
        with tempfile.TemporaryDirectory(prefix="lora-") as tmp:
            trained = train_lora(base_model, rows, tmp, steps, lora_r, seed)
            # log_metrics takes numbers only; device is a label → record it as a tag.
            mlflow.log_metrics({k: v for k, v in trained["metrics"].items()
                                if isinstance(v, (int, float))})
            mlflow.set_tag("device", trained["metrics"].get("device"))
            gguf = os.path.join(tmp, f"{output_name}.gguf")
            convert_to_gguf(trained["adapter_dir"], base_model, gguf)
            mv = register_version(output_name, gguf, run_id, base_model,
                                  dataset_name, dataset_version)
        mlflow.set_tag("registered_version", mv["version"])
        return {"run_id": run_id, "model": mv, "metrics": trained["metrics"], "params": params}


if __name__ == "__main__":
    # CLI: python -m flows.finetune <dataset_name> <dataset_version> <output_name> [steps]
    a = sys.argv
    out = finetune_flow(
        dataset_name=a[1], dataset_version=a[2], output_name=a[3],
        steps=int(a[4]) if len(a) > 4 else 10,
    )
    print(json.dumps(out, indent=2))
