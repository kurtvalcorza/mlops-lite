#!/usr/bin/env python3
"""Agent HTTP runtime drill (020 US3, T414 — FR-205; quickstart §US3).

Measures the agent's real duties on hardware, against a RUNNING agent, producing one
RuntimeBaselineRecord (data-model.md) per run:

  * stream TTFT + inter-frame stall count under concurrent `/health` polling
  * multipart round-trip latency (vision classify, deterministic fixture image)
  * mid-stream client disconnect: the child must be unaffected and the NEXT request clean
  * preempt-during-stream: a `?preempt=true` swap arriving mid-stream — records the observed
    409-vs-drain behavior (must match the documented lease semantics on both runtimes)

023 US6 retired the dual-runtime switch (keep-stdlib verdict) — `--runtime` survives as a plain
record label (default `stdlib`):

  python scripts/agent_stream_drill.py --report

`--report` appends a markdown RuntimeBaselineRecord to the runbook doc; without it the record
prints as JSON. Baselines (the 017/018 runbook stream numbers) come in as flags; a missing
baseline records the measurement without a pass/fail. The keep/upgrade VERDICT is T415's call
across both records — this tool measures and records.
"""
import argparse
import http.client
import json
import os
import statistics
import sys
import threading
import time
from urllib.parse import urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNBOOK = os.path.join(REPO, "docs", "on-hardware-validation.md")


def _conn(agent_url: str, timeout: float):
    u = urlparse(agent_url)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def _poller(agent_url: str, stop: threading.Event, results: list, hz: float):
    """Concurrent /health polling load — the drill measures streams under it, not in a vacuum."""
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            c = _conn(agent_url, timeout=5)
            c.request("GET", "/health")
            ok = c.getresponse().status == 200
            c.close()
        except OSError:
            ok = False
        results.append((ok, (time.perf_counter() - t0) * 1000))
        stop.wait(1.0 / hz)


def stream_once(agent_url: str, engine: str, verb: str, body: dict, *, stall_gap_s: float,
                timeout: float, max_frames: int = 0) -> dict:
    """One streamed request, reading SSE frames incrementally. Returns ttft_ms, stalls, frames,
    total_ms; `max_frames > 0` closes the connection mid-stream after that many frames (the
    disconnect scenario) and reports disconnected=True."""
    c = _conn(agent_url, timeout=timeout)
    payload = json.dumps(body)
    t0 = time.perf_counter()
    c.request("POST", f"/engines/{engine}/{verb}/stream", body=payload,
              headers={"Content-Type": "application/json"})
    resp = c.getresponse()
    if resp.status != 200:
        raise SystemExit(f"stream request failed: HTTP {resp.status} "
                         f"{resp.read(300).decode(errors='replace')}")
    ttft_ms = None
    stalls = 0
    frames = 0
    last = time.perf_counter()
    disconnected = False
    while True:
        line = resp.fp.readline()
        if not line:
            break
        if not line.startswith(b"data:"):
            continue  # SSE keepalives/blank separators don't count as frames
        now = time.perf_counter()
        if ttft_ms is None:
            ttft_ms = (now - t0) * 1000
        elif (now - last) > stall_gap_s:
            stalls += 1
        last = now
        frames += 1
        if max_frames and frames >= max_frames:
            disconnected = True
            break
    total_ms = (time.perf_counter() - t0) * 1000
    c.close()  # mid-stream on the disconnect scenario; a no-op after a drained stream
    if ttft_ms is None:
        raise SystemExit("stream produced no data frames")
    return {"ttft_ms": ttft_ms, "stalls": stalls, "frames": frames, "total_ms": total_ms,
            "disconnected": disconnected}


def measure_stream(agent_url: str, engine: str, verb: str, body: dict, *, runs: int,
                   stall_gap_s: float, poll_hz: float, timeout: float) -> dict:
    stop = threading.Event()
    polls = []
    poller = threading.Thread(target=_poller, args=(agent_url, stop, polls, poll_hz), daemon=True)
    poller.start()
    try:
        singles = [stream_once(agent_url, engine, verb, body,
                               stall_gap_s=stall_gap_s, timeout=timeout) for _ in range(runs)]
    finally:
        stop.set()
        poller.join(timeout=5)
    poll_fail = sum(1 for ok, _ in polls if not ok)
    return {
        "runs": runs,
        "ttft_ms": round(statistics.median(s["ttft_ms"] for s in singles), 1),
        "ttft_ms_max": round(max(s["ttft_ms"] for s in singles), 1),
        "stalls": max(s["stalls"] for s in singles),
        "stall_gap_s": stall_gap_s,
        "frames_median": int(statistics.median(s["frames"] for s in singles)),
        "health_polls": len(polls),
        "health_poll_failures": poll_fail,
        "health_poll_ms_median": round(statistics.median(ms for _, ms in polls), 1) if polls
        else None,
    }


def measure_disconnect(agent_url: str, engine: str, verb: str, body: dict, *,
                       stall_gap_s: float, timeout: float) -> dict:
    """Disconnect mid-stream (after 2 frames), then run one FULL stream — clean == ok."""
    cut = stream_once(agent_url, engine, verb, body, stall_gap_s=stall_gap_s,
                      timeout=timeout, max_frames=2)
    t0 = time.perf_counter()
    follow = stream_once(agent_url, engine, verb, body, stall_gap_s=stall_gap_s, timeout=timeout)
    return {"disconnect_ok": cut["disconnected"] and follow["frames"] >= 1,
            "next_request_ttft_ms": round(follow["ttft_ms"], 1),
            "recovered_in_ms": round((time.perf_counter() - t0) * 1000, 1)}


def _vision_multipart():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from capture_goldens import _golden_png, _multipart_image  # same fixture as the US2 gate
    return _multipart_image(_golden_png())


def measure_multipart(agent_url: str, *, runs: int, timeout: float, preempt: bool = False) -> dict:
    """Vision classify RTT with the golden-style deterministic fixture image. `preempt=True`
    sends `?preempt=true` so the leg can run while another serving tenant is resident (the
    stream leg leaves the LLM holding the slot — one model in VRAM): run 1 measures the
    swap+load, warm runs are same-tenant no-ops."""
    body, ctype = _vision_multipart()
    path = "/engines/vision/classify" + ("?preempt=true" if preempt else "")
    times = []
    for _ in range(runs):
        c = _conn(agent_url, timeout=timeout)
        t0 = time.perf_counter()
        c.request("POST", path, body=body,
                  headers={"Content-Type": ctype})
        resp = c.getresponse()
        data = resp.read()
        c.close()
        if resp.status != 200:
            raise SystemExit(f"multipart classify failed: HTTP {resp.status} "
                             f"{data[:200].decode(errors='replace')}")
        times.append((time.perf_counter() - t0) * 1000)
    return {"runs": runs, "multipart_ms": round(statistics.median(times), 1),
            "multipart_ms_max": round(max(times), 1)}


def measure_preempt_during_stream(agent_url: str, engine: str, verb: str, body: dict, *,
                                  target_engine: str, stall_gap_s: float, timeout: float) -> dict:
    """Fire a `?preempt=true` load of `target_engine` while a stream is mid-flight; record the
    observed behavior (409 refused vs drained-then-served — either can be correct per the lease
    semantics; what matters is it MATCHES the documented behavior on both runtimes).

    The contender must be a GPU tenant or no contention exists (CPU engines are off-lease). On
    this platform the GPU verbs besides the streaming LLM are multipart (vision classify / asr
    transcribe), so the default contender is a vision classify built from the golden fixture; a
    JSON `{"rows": …}` predict is kept for any JSON-shaped target."""
    result = {}
    if target_engine == "vision":
        contender_body, contender_ctype = _vision_multipart()
        contender_path = "/engines/vision/classify?preempt=true"
    else:
        contender_body = json.dumps({"rows": [{}]})
        contender_ctype = "application/json"
        contender_path = f"/engines/{target_engine}/predict?preempt=true"

    def preempt():
        time.sleep(0.3)  # let the stream get going
        t0 = time.perf_counter()
        try:
            c = _conn(agent_url, timeout=timeout)
            c.request("POST", contender_path, body=contender_body,
                      headers={"Content-Type": contender_ctype})
            resp = c.getresponse()
            result["status"] = resp.status
            result["body"] = resp.read(300).decode(errors="replace")
            c.close()
        except OSError as e:
            result["status"] = None
            result["body"] = str(e)
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    t = threading.Thread(target=preempt, daemon=True)
    t.start()
    stream = stream_once(agent_url, engine, verb, body, stall_gap_s=stall_gap_s, timeout=timeout)
    t.join(timeout=timeout)
    behavior = ("refused-409" if result.get("status") == 409 else
                "served" if result.get("status") == 200 else f"status={result.get('status')}")
    return {"preempt_status": result.get("status"), "behavior": behavior,
            "preempt_latency_ms": result.get("latency_ms"),
            "stream_completed_frames": stream["frames"]}


def build_record(runtime: str, stream: dict, multipart, disconnect: dict, preempt,
                 baselines: dict) -> dict:
    record = {
        "runtime": runtime,
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ttft_ms": stream["ttft_ms"],
        "stalls": stream["stalls"],
        "stream": stream,
        "multipart_ms": (multipart or {}).get("multipart_ms"),
        "disconnect_ok": disconnect["disconnect_ok"],
        "disconnect": disconnect,
        "swap_contention": preempt,
        "baselines": baselines,
    }
    misses = []
    if baselines.get("ttft_ms") is not None and stream["ttft_ms"] > baselines["ttft_ms"]:
        misses.append(f"ttft_ms {stream['ttft_ms']} > baseline {baselines['ttft_ms']}")
    if baselines.get("stalls_max") is not None and stream["stalls"] > baselines["stalls_max"]:
        misses.append(f"stalls {stream['stalls']} > baseline {baselines['stalls_max']}")
    if (baselines.get("multipart_ms") is not None and multipart
            and multipart["multipart_ms"] > baselines["multipart_ms"]):
        misses.append(f"multipart_ms {multipart['multipart_ms']} > "
                      f"baseline {baselines['multipart_ms']}")
    if not disconnect["disconnect_ok"]:
        misses.append("mid-stream disconnect did not recover cleanly")
    record["misses"] = misses
    record["meets_baselines"] = not misses
    return record


def append_runbook(record: dict, path: str) -> None:
    block = (f"\n### RuntimeBaselineRecord — `{record['runtime']}` ({record['measured_at']})\n\n"
             f"```json\n{json.dumps(record, indent=2)}\n```\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(block)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent-url", default=os.getenv("AGENT_URL", "http://127.0.0.1:8100"))
    ap.add_argument("--runtime", required=True,
                    help="label for the record — the AGENT_RUNTIME the agent is running")
    ap.add_argument("--engine", default="llm")
    ap.add_argument("--verb", default="infer")
    ap.add_argument("--prompt", default="Count from 1 to 30, one number per line.")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--stall-gap-s", type=float, default=1.0,
                    help="an inter-frame gap above this counts as a stall")
    ap.add_argument("--poll-hz", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--skip-multipart", action="store_true",
                    help="skip the vision leg (no GPU box / vision model)")
    ap.add_argument("--skip-preempt", action="store_true")
    ap.add_argument("--multipart-preempt", action="store_true",
                    help="send the multipart leg with ?preempt=true (needed when the stream leg"
                         " leaves the LLM resident — one model in VRAM)")
    ap.add_argument("--preempt-target", default="vision",
                    help="GPU engine whose ?preempt=true load contends with the stream")
    ap.add_argument("--baseline-ttft-ms", type=float, default=None)
    ap.add_argument("--baseline-stalls-max", type=int, default=None)
    ap.add_argument("--baseline-multipart-ms", type=float, default=None)
    ap.add_argument("--report", action="store_true",
                    help=f"append the record to {os.path.relpath(RUNBOOK, REPO)}")
    ap.add_argument("--runbook", default=RUNBOOK)
    args = ap.parse_args(argv)

    agent = args.agent_url.rstrip("/")
    body = {"prompt": args.prompt, "max_tokens": args.max_tokens, "temperature": 0.1}
    print(f"[drill] runtime={args.runtime} agent={agent}", file=sys.stderr)

    stream = measure_stream(agent, args.engine, args.verb, body, runs=args.runs,
                            stall_gap_s=args.stall_gap_s, poll_hz=args.poll_hz,
                            timeout=args.timeout)
    print(f"[drill] stream: ttft={stream['ttft_ms']}ms stalls={stream['stalls']} "
          f"(polls={stream['health_polls']}, poll_failures={stream['health_poll_failures']})",
          file=sys.stderr)
    # Leg order matters under "one model in VRAM": every LLM leg runs while the LLM is the
    # resident tenant; the vision multipart leg runs LAST (with --multipart-preempt when the
    # stream legs leave the LLM holding the slot).
    disconnect = measure_disconnect(agent, args.engine, args.verb, body,
                                    stall_gap_s=args.stall_gap_s, timeout=args.timeout)
    print(f"[drill] disconnect: ok={disconnect['disconnect_ok']}", file=sys.stderr)
    preempt = None
    if not args.skip_preempt:
        preempt = measure_preempt_during_stream(
            agent, args.engine, args.verb, body, target_engine=args.preempt_target,
            stall_gap_s=args.stall_gap_s, timeout=args.timeout)
        print(f"[drill] preempt-during-stream: {preempt['behavior']}", file=sys.stderr)
    multipart = None
    if not args.skip_multipart:
        multipart = measure_multipart(agent, runs=args.runs, timeout=args.timeout,
                                      preempt=args.multipart_preempt)
        print(f"[drill] multipart: {multipart['multipart_ms']}ms median", file=sys.stderr)

    baselines = {"ttft_ms": args.baseline_ttft_ms, "stalls_max": args.baseline_stalls_max,
                 "multipart_ms": args.baseline_multipart_ms, "stall_gap_s": args.stall_gap_s}
    record = build_record(args.runtime, stream, multipart, disconnect, preempt, baselines)
    print(json.dumps(record, indent=2))
    if args.report:
        append_runbook(record, args.runbook)
        print(f"[drill] record appended to {args.runbook}", file=sys.stderr)
    return 0 if record["meets_baselines"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
