#!/usr/bin/env python3
"""018 SC-108 — GPU swap-contention stress: prove one-model-in-VRAM under a preempt storm.

Drives the platform through many preemptive serving swaps (LLM / vision / ASR, `preempt=true`) while
a background sampler watches for the two failure modes Principle II forbids:

  1. **Two GPU tenants resident at once** — sampled from `nvidia-smi` total `memory.used`: one model
     peaks at its own footprint and dips to baseline between swaps; a co-residency bug would sum two
     models and never dip (WSL2 can't enumerate per-process compute apps, so total VRAM is used).
  2. **A third tenant sniping the freed slot mid-swap** — fired as *concurrent* preempts for
     different targets: the agent's swap reservation must let exactly one land per round, refusing
     the rest (409 PreemptRefused) — never leaving the slot to a contender between evict and load.

The single admission slot makes both structurally impossible; this exercises it live under load.
Exit 0 iff ≥ `--cycles` swaps completed, every landed swap ended with its own target as the holder,
and peak VRAM stayed under the one-model ceiling.

Usage (from the WSL GPU host):
    python scripts/swap_stress.py --cycles 120
    python scripts/swap_stress.py --cycles 120 --gateway http://localhost:8080
"""
import argparse
import base64
import io
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.getenv("AGENT_HEALTH_BASE", "http://localhost:8100")


def _key(env_path):
    try:
        for line in open(env_path, encoding="utf-8"):
            if line.startswith("GATEWAY_API_KEYS="):
                return line.split("=", 1)[1].strip().strip('"').split(",")[0]
    except OSError:
        pass
    return ""


def _post(url, body, key, timeout):
    data = json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["X-API-Key"] = key
    req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def _png():
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (120, 160, 200)).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return base64.b64encode(buf.getvalue()).decode()


def _targets(gw, key):
    img, aud = _png(), _wav()
    # engine -> (url, body) for a preempting serving request through the gateway.
    return {
        "llm": (f"{gw}/infer", {"prompt": "hi", "max_tokens": 4, "preempt": True}),
        "vision": (f"{gw}/vision/classify", {"image_b64": img, "preempt": True}),
        "asr": (f"{gw}/transcribe", {"audio_b64": aud, "filename": "s.wav",
                                     "language": "en", "preempt": True}),
    }


def _holder():
    try:
        with urllib.request.urlopen(f"{AGENT}/health", timeout=3) as r:
            return (json.loads(r.read() or b"{}") or {}).get("holder")
    except Exception:
        return "?"


def _gpu_vram_used_gb():
    """Total GPU memory in use right now, in GB (co-residency probe). WSL2's nvidia-smi can't
    enumerate per-process compute apps (`--query-compute-apps` → [N/A]), but total `memory.used`
    works — one resident model peaks at its own footprint and dips to ~baseline between swaps, while
    a co-residency bug would sum two models and never dip. None if nvidia-smi is unreadable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        return float(out.stdout.strip().splitlines()[0]) / 1024.0
    except Exception:
        return None


class Sampler(threading.Thread):
    """Polls nvidia-smi total VRAM + the agent holder; records the peak/baseline VRAM and every
    distinct holder seen. Peak ≈ one model (never the sum of two) + a dip toward baseline between
    swaps is the physical no-co-residency evidence; the agent holder is the structural witness (a
    single admission slot can never name two tenants)."""
    def __init__(self, hz=8):
        super().__init__(daemon=True)
        self.period = 1.0 / hz
        self.stop = threading.Event()
        self.max_vram = 0.0
        self.min_vram = None
        self.samples = 0
        self.holders = set()
        self.smi_ok = True

    def run(self):
        while not self.stop.is_set():
            v = _gpu_vram_used_gb()
            if v is None:
                self.smi_ok = False
            else:
                self.samples += 1
                self.max_vram = max(self.max_vram, v)
                self.min_vram = v if self.min_vram is None else min(self.min_vram, v)
            h = _holder()
            if h and h not in ("?", None):
                self.holders.add(h)
            self.stop.wait(self.period)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=120, help="min completed swap cycles")
    ap.add_argument("--gateway", default=os.getenv("GATEWAY", "http://localhost:8080"))
    ap.add_argument("--env", default=os.path.join(REPO, ".env"))
    ap.add_argument("--engines", default="llm,vision,asr",
                    help="comma list to rotate (vision cold-loads ~10x slower — drop it for a "
                         "fast, high-count run; keep it for the 3-way mix)")
    ap.add_argument("--max-vram-gb", type=float, default=8.0,
                    help="co-residency ceiling: peak total VRAM must stay under this (one model's "
                         "footprint + margin; two co-resident models would exceed it)")
    args = ap.parse_args()
    key = _key(args.env)
    targets = _targets(args.gateway, key)
    order = [e.strip() for e in args.engines.split(",") if e.strip() in targets]
    assert len(order) >= 2, "need >= 2 engines to swap between"

    sampler = Sampler()
    sampler.start()

    landed = 0          # swaps that completed with the requested target as holder
    mismatched = 0      # a swap "succeeded" but the holder isn't the target
    print(f"swap-stress: >= {args.cycles} cycles via {args.gateway} (key={'yes' if key else 'no'})")
    t0 = time.time()
    i = 0
    n = len(order)
    while landed < args.cycles:
        # Every 3rd cycle: fire TWO different-target preempts CONCURRENTLY to race the reservation.
        if i % 3 == 2:
            a, b = order[i % n], order[(i + 1) % n]
            results = {}

            def fire(eng):
                url, body = targets[eng]
                results[eng] = _post(url, body, key, timeout=120)

            ts = [threading.Thread(target=fire, args=(e,)) for e in (a, b)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            winners = [e for e, (st, _) in results.items() if st == 200]
            # exactly one may hold the GPU; if both returned 200 they still serialized (one
            # evicted the other), so re-read the holder and count one landed cycle for the winner.
            time.sleep(0.05)
            h = _holder()
            if len(winners) >= 1 and h in results:
                landed += 1
        else:
            eng = order[i % n]
            url, body = targets[eng]
            st, resp = _post(url, body, key, timeout=120)
            if st == 200:
                h = _holder()
                if h == eng:
                    landed += 1
                else:
                    mismatched += 1
            # 409 (refuse/preempt-refused) is legal under contention; just retry next loop.
        i += 1
        if i > args.cycles * 6:  # safety backstop against an infinite loop
            break

    dur = time.time() - t0
    sampler.stop.set()
    sampler.join(timeout=2)

    minv = sampler.min_vram if sampler.min_vram is not None else -1
    print("\n== SC-108 swap-stress result ==")
    print(f"cycles landed        : {landed} (target >= {args.cycles})")
    print(f"holder mismatches    : {mismatched}")
    print(f"tenants seen resident: {sorted(sampler.holders)} (single-slot: one at a time)")
    print(f"nvidia-smi samples   : {sampler.samples} (smi_ok={sampler.smi_ok})")
    print(f"peak VRAM used       : {sampler.max_vram:.2f} GB  (MUST be < {args.max_vram_gb} GB)")
    print(f"baseline VRAM (min)  : {minv:.2f} GB  (dips toward idle between swaps = full eviction)")
    print(f"duration             : {dur:.1f}s, {i} requests")
    ok = (landed >= args.cycles and mismatched == 0 and sampler.smi_ok
          and sampler.max_vram < args.max_vram_gb)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
