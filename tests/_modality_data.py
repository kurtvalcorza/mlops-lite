"""Stdlib-only synthetic data for the 010 modality fine-tune tests (no PIL / numpy / wave deps in the
test process). Builds tiny but valid artifacts in the exact shapes the flows parse:

  - `png_b64(rgb, size)`  → a solid-color RGB PNG (vision dataset `{image_b64, label}`)
  - `wav_b64(freq, ...)`  → a 16 kHz mono 16-bit PCM WAV (ASR dataset `{audio_b64, text}`)

Keeping these dependency-free means the tests can construct their datasets anywhere pytest runs (they
still SKIP unless the trainer daemon is up — the actual training needs the GPU venv).
"""
import array
import base64
import io
import math
import struct
import wave
import zlib


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_b64(rgb=(255, 0, 0), size: int = 8) -> str:
    """A `size`×`size` solid-color RGB PNG as base64 (8-bit, color type 2). The vision flow's
    Resize(256)/CenterCrop(224) upsamples it, so a tiny image trains fine."""
    w = h = size
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * w           # filter byte 0 + w RGB pixels
    idat = zlib.compress(row * h)
    png = sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
    return base64.b64encode(png).decode()


def wav_b64(seconds: float = 0.3, freq: float = 220.0, sr: int = 16000) -> str:
    """A short 16 kHz mono 16-bit PCM WAV (a sine tone) as base64 — the shape `asr_finetune` decodes."""
    n = int(seconds * sr)
    samples = array.array(
        "h", (int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / sr)) for i in range(n)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
    return base64.b64encode(buf.getvalue()).decode()
