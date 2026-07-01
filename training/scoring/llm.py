"""Transient llama.cpp LLM scorer (015) — score the SERVED GGUF, not the unquantized HF weights (D5).

At registration the LoRA adapter is already converted to GGUF (the artifact llama-server serves via
`--lora`). This scorer loads the **base GGUF + that LoRA-GGUF adapter** in a short-lived llama-server
(the same binary + flags `serving/llama/{run,verify_lora}.sh` use), generates greedily over the QA
benchmark prompts, then **tears the server down** so VRAM is freed promptly (load → score → free, D5 /
Edge Case). It measures the exact quantized artifact that gets served.

`make_predict_fn(adapter_gguf, base_hf)` returns a `predict_fn(rows, modality, version) -> [text, ...]`
closure the LLM flow calls via `score_and_log` inside its lease hold — **after** `train_lora` freed the
HF training model, so only one model is resident (Principle II). Top-level imports are stdlib-only so
this module imports on any box (the heavy bits — httpx, huggingface_hub — are deferred); the on-hardware
SCs (087/088/092) validate the real load+score path on Kurt's GPU box.
"""
import os
import subprocess
import sys
import time

LLAMA_DIR = os.path.expanduser(os.getenv("LLAMA_DIR", "~/llama.cpp"))
LLAMA_SERVER = os.path.expanduser(
    os.getenv("LLAMA_BIN", os.path.join(LLAMA_DIR, "build", "bin", "llama-server")))
GGUF_DIR = os.path.expanduser(os.getenv("GGUF_DIR", "~/models/gguf"))
SCORER_PORT = int(os.getenv("LLM_SCORER_PORT", "8096"))  # free port: 8081=serving llama child,
# 8090=serving supervisor, 8091-8095=trainer/vision/embed/tabular/asr, 8099=supervise status server.
# 8099 (the earlier default) collided with the supervisor's status HTTP server → the scorer's POST hit
# a GET-only handler and got 501, so LLM score-at-registration silently register-and-warned on-hardware.
DEFAULT_BASE_HF = os.getenv("BASE_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")


def resolve_base_gguf(base_hf: str) -> str:
    """The base model's f16 GGUF path (mirrors serving/llama/verify_lora.sh), converting from the cached
    HF snapshot once if absent. The HF model is already downloaded during training, so the convert is a
    one-time cost the first time a given base is scored."""
    base_gguf = os.path.join(GGUF_DIR, f"{base_hf.replace('/', '_')}-f16.gguf")
    if os.path.exists(base_gguf):
        return base_gguf
    from huggingface_hub import snapshot_download

    os.makedirs(GGUF_DIR, exist_ok=True)
    snap = base_hf if os.path.isdir(base_hf) else snapshot_download(base_hf)
    convert = os.path.join(LLAMA_DIR, "convert_hf_to_gguf.py")
    if not os.path.exists(convert):
        raise FileNotFoundError(f"llama.cpp HF->GGUF converter not found at {convert}")
    proc = subprocess.run([sys.executable, convert, snap, "--outfile", base_gguf, "--outtype", "f16"],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(base_gguf):
        raise RuntimeError(f"base GGUF conversion failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
    return base_gguf


def _wait_ready(base_url: str, proc: subprocess.Popen, timeout: float = 180.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("llama-server exited during startup (scorer)")
        try:
            r = httpx.get(f"{base_url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError("llama-server (scorer) did not become ready in time")


def make_predict_fn(gguf_path: str, base_hf: str = DEFAULT_BASE_HF, *, lora: bool = True,
                    n_predict: int = 32, temperature: float = 0.0,
                    ngl: int = 999, ctx: int = 2048, port: int = SCORER_PORT,
                    server_bin: str = LLAMA_SERVER):
    """Build a `predict_fn` that scores an LLM GGUF via a short-lived llama-server. One VRAM load for the
    whole benchmark; the server is always terminated (load → score → free), so nothing stays resident or
    holds the lease past the fine-tune.

    `lora=True` (default): `gguf_path` is a **LoRA-GGUF adapter** served on top of `base_hf`'s f16 GGUF
    (`-m base --lora adapter`) — the 015 fine-tune shape. `lora=False`: `gguf_path` is a **full-model
    GGUF** served directly (`-m gguf`, `base_hf` ignored) — a baseline/raw registered LLM version rather
    than a fine-tuned adapter (016 shadow-replay). Per-row `max_tokens`/`temperature` override the defaults
    so a replay can reproduce the champion's served decoding settings."""

    def predict_fn(rows, _modality, _version):
        import httpx

        if not os.path.exists(server_bin):
            raise FileNotFoundError(f"llama-server not found at {server_bin} — build llama.cpp first")
        if lora:
            model_args = ["-m", resolve_base_gguf(base_hf), "--lora", gguf_path]
        else:
            model_args = ["-m", gguf_path]  # full-model GGUF — the served artifact itself, no adapter
        base_url = f"http://127.0.0.1:{port}"
        cmd = [server_bin, *model_args, "-ngl", str(ngl),
               "--host", "127.0.0.1", "--port", str(port), "-c", str(ctx)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_ready(base_url, proc)
            out = []
            with httpx.Client(timeout=300) as client:
                for r in rows:
                    resp = client.post(f"{base_url}/completion", json={
                        "prompt": r["prompt"], "n_predict": int(r.get("max_tokens", n_predict)),
                        "temperature": float(r.get("temperature", temperature)), "cache_prompt": False,
                    })
                    resp.raise_for_status()
                    out.append(resp.json().get("content", ""))
            return out
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()

    return predict_fn
