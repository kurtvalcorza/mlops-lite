"""HF Whisper → ggml converter for whisper.cpp serving (010 US3, T192 — FR-093; the new tool).

The increment's one genuinely new piece of tooling — the ASR analogue of the LLM flow's LoRA → GGUF
converter (`flows/finetune.py:convert_to_gguf`). The ASR fine-tune output is a HF-`transformers` Whisper
model (with the LoRA adapter already **merged into the base** by the flow), so this uses whisper.cpp's
**`convert-h5-to-ggml.py`** (the HF route, *not* the OpenAI `.pt` `convert-pt-to-ggml` route — grilled
decision c) to write an f16 `ggml-model.bin`, then quantizes it to **q8_0** with whisper.cpp's `quantize`
binary (Whisper base/small are tiny → q8_0 balances size/accuracy). The result is the `ggml-*.bin`
009's whisper.cpp server loads.

Fails loudly like `convert_to_gguf`: a non-zero converter/quantizer exit or a missing output raises with
a captured stderr tail, so the ASR run registers **no** partial version (FR-093 / Edge: ggml conversion
failure).

Paths are env-overridable (mirroring `LLAMA_DIR`):
  WHISPER_CPP_DIR     whisper.cpp checkout (default ~/whisper.cpp)
  WHISPER_HF_CONVERT  the convert-h5-to-ggml.py script (default $WHISPER_CPP_DIR/models/convert-h5-to-ggml.py)
  WHISPER_QUANTIZE    the quantize binary (default $WHISPER_CPP_DIR/build/bin/quantize)
  WHISPER_ASSETS_DIR  the OpenAI-whisper package dir (mel filters / tokenizer); auto-located if openai-whisper is installed
"""
import os
import subprocess
import sys

WHISPER_CPP_DIR = os.path.expanduser(os.getenv("WHISPER_CPP_DIR", "~/whisper.cpp"))
HF_CONVERT = os.path.expanduser(
    os.getenv("WHISPER_HF_CONVERT", os.path.join(WHISPER_CPP_DIR, "models", "convert-h5-to-ggml.py")))
QUANTIZE_BIN = os.path.expanduser(
    os.getenv("WHISPER_QUANTIZE", os.path.join(WHISPER_CPP_DIR, "build", "bin", "quantize")))


def _log(msg):
    print(msg, flush=True)


def _whisper_assets_dir() -> str:
    """The OpenAI-whisper package dir convert-h5-to-ggml.py reads for mel filters + the tokenizer.

    Prefer WHISPER_ASSETS_DIR; else auto-locate the installed `openai-whisper` package. Raise a clear
    error if neither resolves (so the failure is "install/point at whisper assets", not a cryptic
    converter traceback)."""
    env = os.getenv("WHISPER_ASSETS_DIR")
    if env:
        return os.path.expanduser(env)
    try:
        import whisper  # openai-whisper, if installed
        return os.path.dirname(os.path.abspath(whisper.__file__))
    except Exception as e:
        raise FileNotFoundError(
            "whisper assets dir not found: set WHISPER_ASSETS_DIR to the openai-whisper package dir "
            "(it carries the mel filters + tokenizer convert-h5-to-ggml.py needs)") from e


def _run(cmd: list, what: str) -> None:
    _log(f"{what}: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed (rc={proc.returncode}):\n{(proc.stderr or '')[-1500:]}")


def convert_whisper_to_ggml(hf_model_dir: str, out_path: str, quant: str = "q8_0") -> str:
    """Convert a (LoRA-merged) HF Whisper model dir → a quantized `ggml-*.bin` at `out_path`.

    Two steps, each fail-loud (no partial version on failure — FR-093):
      1. convert-h5-to-ggml.py  → an f16 `ggml-model.bin` in out_path's directory
      2. quantize <f16> <out_path> q8_0
    Returns `out_path`. The caller (asr_finetune) uploads it to MinIO + registers the version.
    """
    if not os.path.exists(HF_CONVERT):
        raise FileNotFoundError(f"whisper.cpp HF converter not found at {HF_CONVERT} "
                                f"(set WHISPER_HF_CONVERT or WHISPER_CPP_DIR)")
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    assets = _whisper_assets_dir()

    # Step 1: HF → f16 ggml. convert-h5-to-ggml.py writes `ggml-model.bin` into out_dir.
    _run([sys.executable, HF_CONVERT, hf_model_dir, assets, out_dir], "HF -> ggml (f16)")
    f16 = os.path.join(out_dir, "ggml-model.bin")
    if not os.path.exists(f16):
        raise RuntimeError(f"HF -> ggml produced no {f16} (converter exited 0 but wrote nothing)")

    # Step 2: quantize f16 → q8_0 (the served artifact). Skip if quant is 'f16'/'none' (keep f16).
    if quant in ("", "f16", "none"):
        if os.path.abspath(f16) != os.path.abspath(out_path):
            os.replace(f16, out_path)
        _log(f"ggml (f16) written: {out_path} ({os.path.getsize(out_path)} bytes)")
        return out_path
    if not os.path.exists(QUANTIZE_BIN):
        raise FileNotFoundError(f"whisper.cpp quantize binary not found at {QUANTIZE_BIN} "
                                f"(build whisper.cpp or set WHISPER_QUANTIZE; or pass quant='f16')")
    _run([QUANTIZE_BIN, f16, out_path, quant], f"quantize -> {quant}")
    if not os.path.exists(out_path):
        raise RuntimeError(f"quantize produced no {out_path}")
    try:
        os.remove(f16)  # drop the intermediate f16; the q8_0 file is the artifact
    except OSError:
        pass
    _log(f"ggml ({quant}) written: {out_path} ({os.path.getsize(out_path)} bytes)")
    return out_path


if __name__ == "__main__":
    # CLI: python convert_whisper_to_ggml.py <hf_model_dir> <out_path> [quant]
    a = sys.argv
    if len(a) < 3:
        print(__doc__)
        sys.exit(2)
    convert_whisper_to_ggml(a[1], a[2], a[3] if len(a) > 3 else "q8_0")
