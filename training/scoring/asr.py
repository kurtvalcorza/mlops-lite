"""Transient whisper.cpp ASR scorer (015) — score the SERVED ggml, not the unquantized HF weights (D6).

ASR is the LLM's twin: HF-trained, merged, then converted to a quantized `ggml-*.bin` (the artifact
009's whisper.cpp server loads). This scorer transcribes the WER benchmark clips through that **served
ggml** via a short-lived `whisper-cli` (load → score → free per clip, so VRAM never lingers), then the
glue computes WER against each clip's `text`. It measures the exact artifact that gets served.

`make_predict_fn(ggml_path)` returns a `predict_fn(rows, modality, version) -> [text, ...]` closure the
ASR flow calls via `score_and_log` inside its lease hold — **after** `_train_and_merge` freed the HF
training model, so only one model is resident (Principle II). Top-level imports are stdlib-only; the
on-hardware SCs (087/088/092) validate the real transcription path on Kurt's GPU box.
"""
import base64
import os
import subprocess
import tempfile

WHISPER_DIR = os.path.expanduser(os.getenv("WHISPER_DIR", "~/whisper.cpp"))
WHISPER_CLI = os.path.expanduser(
    os.getenv("WHISPER_CLI", os.path.join(WHISPER_DIR, "build", "bin", "whisper-cli")))


def make_predict_fn(ggml_path: str, *, cli_bin: str = WHISPER_CLI, use_gpu: bool = True):
    """Build a `predict_fn` that transcribes each benchmark clip through the served `ggml_path` via a
    transient `whisper-cli` (`-nt -np` → bare transcription on stdout, no timestamps/progress). The
    benchmark `audio_b64` is already a 16 kHz mono PCM WAV, so it is written straight to a temp file.

    whisper.cpp uses the GPU by default when built with CUDA (the 009 whisper-server supervisor passes no
    GPU flag either — it has no per-layer offload like llama.cpp); `use_gpu=False` adds `-ng/--no-gpu` to
    force CPU."""

    def predict_fn(rows, _modality, _version):
        if not os.path.exists(cli_bin):
            raise FileNotFoundError(f"whisper-cli not found at {cli_bin} — build whisper.cpp first")
        if not os.path.exists(ggml_path):
            raise FileNotFoundError(f"served ggml not found at {ggml_path}")
        out = []
        with tempfile.TemporaryDirectory(prefix="asr-score-") as tmp:
            for i, r in enumerate(rows):
                wav = os.path.join(tmp, f"clip_{i}.wav")
                with open(wav, "wb") as f:
                    f.write(base64.b64decode(r["audio_b64"], validate=True))
                cmd = [cli_bin, "-m", ggml_path, "-f", wav, "-nt", "-np"]
                lang = r.get("language")  # 016: replay the champion's served language, not the default
                if lang and lang != "auto":
                    cmd += ["-l", str(lang)]
                if not use_gpu:
                    cmd.append("-ng")  # --no-gpu (whisper.cpp); default is GPU when built with CUDA
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"whisper-cli failed on clip {i} (rc={proc.returncode}):\n{proc.stderr[-800:]}")
                out.append(proc.stdout.strip())
        return out

    return predict_fn
