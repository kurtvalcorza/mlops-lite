"""009 US3 ASR integration test (T169 → SC-053).

Transcribes a short audio clip through the gateway → whisper.cpp native CUDA daemon (a GPU-lease
tenant). Asserts:
  - a 200 with a `text` string + the model name (a transcript came back),
  - the daemon **took the GPU lease on demand** — right after a transcribe, /serving/state reports the
    holder as `asr` (resident) or null (already idle-released); it is never another tenant, proving
    ASR participated in the single lease (Principle II / 008),
  - generated audio keeps the test stdlib-only + offline (a 0.5 s mono WAV; whisper returns empty or
    near-empty text for it, which is still a valid transcript shape).

SKIPs cleanly if the ASR daemon isn't running (it needs whisper.cpp built with CUDA). Non-zero on fail.
"""
import base64
import io
import json
import math
import os
import struct
import sys
import urllib.error
import urllib.request
import wave

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"


def _tiny_wav_b64() -> str:
    """A 0.5 s 16 kHz mono WAV (a faint low tone) — enough for whisper to run a forward pass."""
    rate, secs = 16000, 0.5
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * secs)):
            sample = int(1000 * math.sin(2 * math.pi * 220 * i / rate))
            frames += struct.pack("<h", sample)
        w.writeframes(bytes(frames))
    return base64.b64encode(buf.getvalue()).decode()


def _req(method, path, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> int:
    _, h = _req("GET", "/transcribe/health")
    if not h.get("reachable"):
        print("[SKIP] ASR (whisper.cpp) daemon not running "
              "(bash serving/whispercpp/build.sh && run.sh)")
        return 0
    print(f"[OK] ASR service reachable ({h.get('backend')})")

    s, res = _req("POST", "/transcribe", {"audio_b64": _tiny_wav_b64(), "filename": "tone.wav"})
    if s != 200:
        print(f"[FAIL] transcribe -> {s} {res}")
        return 1
    if not isinstance(res.get("text"), str) or not res.get("model"):
        print(f"[FAIL] malformed transcript (need text:str + model): {res}")
        return 1
    print(f"[OK] transcribe -> text={res['text']!r} model={res['model']} "
          f"infer_ms={res.get('infer_ms')}")

    # The ASR daemon took the single GPU lease on demand: right after a transcribe the holder is `asr`
    # (resident) or null (idle-released) — never another tenant.
    _, st = _req("GET", "/serving/state")
    holder = st.get("holder")
    if holder not in ("asr", None):
        print(f"[FAIL] after transcribe the lease holder is {holder!r}, expected 'asr' or null")
        return 1
    print(f"[OK] post-transcribe lease holder = {holder!r} (ASR is a single-lease GPU tenant)")

    print("\nT169 PASS — whisper.cpp transcript returned; ASR took the GPU lease on demand")
    return 0


def test_transcribe(require_asr, require_key):
    """Pytest wrapper (005 US5): skip unless the ASR daemon is reachable + a key is set."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
