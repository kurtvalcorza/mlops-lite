#!/usr/bin/env python3
"""Golden request/response capture + replay for the child swaps (020 T409, US2 — FR-203).

"Byte-compatible" is only enforceable with recorded bytes (research R6). This tool captures,
per engine, the canonical verb exchange **at the agent boundary** (the children's only
consumer, FR-177) into `tests/goldens/<engine>/`, and replays it after a child swap, diffing
status + content type + body bytes exactly. Any diff fails the swap gate, not production.

    python scripts/capture_goldens.py --engine vision            # capture (pre-swap)
    python scripts/capture_goldens.py --engine vision --replay   # gate (post-swap)

Golden layout (data-model.md GoldenSet): `request.json` (path/method/content type) +
`request.body` (the exact bytes; the vision multipart uses a fixed boundary so capture and
replay send identical requests) + `response.json` (status/content type) + `response.body` +
`probe.json`. The probe rides the agent's `/engines/<e>/health`: only its HTTP status and `ok`
field are recorded — the full health payload carries live VRAM numbers, which are not
byte-stable — so the probe golden pins "the child came up and reports available", while the
byte-diff applies to the verb exchange. Goldens are environment artifacts, captured and
replayed on the same machine/session (model-output floats are hardware-sensitive) — they are
NOT committed fixtures.
"""
import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDENS_ROOT = os.path.join(REPO, "tests", "goldens")
BOUNDARY = "mlops-golden-boundary-7f3a9c"  # fixed => capture and replay send identical bytes

EMBED_TEXTS = ["the quick brown fox jumps over the lazy dog", "golden replay probe"]
TABULAR_ROWS = [{}, {"f0": 1.0, "f1": 0.5}]  # unknown features are ignored, missing default 0.0


def _golden_png() -> bytes:
    """A small deterministic RGB image (PIL is in the host venv — the vision child's own dep)."""
    from PIL import Image
    img = Image.new("RGB", (64, 64))
    img.putdata([(x * 4 % 256, y * 4 % 256, (x + y) * 2 % 256)
                 for y in range(64) for x in range(64)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _multipart_image(png: bytes) -> tuple:
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="golden.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + png + f"\r\n--{BOUNDARY}--\r\n".encode()
    return body, f"multipart/form-data; boundary={BOUNDARY}"


def build_request(engine: str) -> dict:
    """The canonical request per engine: {path, method, content_type, body}."""
    if engine == "vision":
        body, ctype = _multipart_image(_golden_png())
        return {"path": "/engines/vision/classify", "method": "POST",
                "content_type": ctype, "body": body}
    if engine == "embed":
        return {"path": "/engines/embed/embed", "method": "POST",
                "content_type": "application/json",
                "body": json.dumps({"texts": EMBED_TEXTS}).encode()}
    if engine == "tabular":
        return {"path": "/engines/tabular/predict", "method": "POST",
                "content_type": "application/json",
                "body": json.dumps({"rows": TABULAR_ROWS}).encode()}
    raise SystemExit(f"unknown engine {engine!r} (vision|embed|tabular)")


def _send(agent_url: str, req: dict, timeout: float) -> dict:
    """One exchange -> {status, content_type, body}. HTTP errors are captured, not raised —
    a 4xx/5xx response is still a (failed) golden comparison, with the body kept for the diff."""
    r = urllib.request.Request(agent_url + req["path"], data=req["body"],
                               headers={"Content-Type": req["content_type"]},
                               method=req["method"])
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return {"status": resp.status,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "body": resp.read()}
    except urllib.error.HTTPError as e:
        return {"status": e.code,
                "content_type": e.headers.get("Content-Type", "") if e.headers else "",
                "body": e.read()}


def _probe(agent_url: str, engine: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(f"{agent_url}/engines/{engine}/health",
                                    timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
            return {"status": resp.status, "ok": payload.get("ok")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ok": None}


def capture(engine: str, agent_url: str, root: str, timeout: float) -> str:
    req = build_request(engine)
    resp = _send(agent_url, req, timeout)
    if resp["status"] != 200:
        raise SystemExit(f"capture aborted: {engine} {req['path']} returned {resp['status']} "
                         f"({resp['body'][:200]!r}) — capture goldens against a HEALTHY pre-swap "
                         "child only")
    outdir = os.path.join(root, engine)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "request.json"), "w", encoding="utf-8") as fh:
        json.dump({k: req[k] for k in ("path", "method", "content_type")}, fh, indent=2)
    with open(os.path.join(outdir, "request.body"), "wb") as fh:
        fh.write(req["body"])
    with open(os.path.join(outdir, "response.json"), "w", encoding="utf-8") as fh:
        json.dump({"status": resp["status"], "content_type": resp["content_type"]}, fh, indent=2)
    with open(os.path.join(outdir, "response.body"), "wb") as fh:
        fh.write(resp["body"])
    with open(os.path.join(outdir, "probe.json"), "w", encoding="utf-8") as fh:
        json.dump(_probe(agent_url, engine, timeout), fh, indent=2)
    return outdir


def replay(engine: str, agent_url: str, root: str, timeout: float) -> list:
    """Replay the recorded request; return the list of drift descriptions (empty == gate pass)."""
    outdir = os.path.join(root, engine)
    try:
        with open(os.path.join(outdir, "request.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(os.path.join(outdir, "request.body"), "rb") as fh:
            body = fh.read()
        with open(os.path.join(outdir, "response.json"), encoding="utf-8") as fh:
            want = json.load(fh)
        with open(os.path.join(outdir, "response.body"), "rb") as fh:
            want_body = fh.read()
        with open(os.path.join(outdir, "probe.json"), encoding="utf-8") as fh:
            want_probe = json.load(fh)
    except FileNotFoundError as e:
        raise SystemExit(f"no golden set for {engine!r} under {outdir} — capture first") from e

    got = _send(agent_url, {**meta, "body": body}, timeout)
    diffs = []
    if got["status"] != want["status"]:
        diffs.append(f"status: golden {want['status']} != replayed {got['status']}")
    if got["content_type"] != want["content_type"]:
        diffs.append(f"content-type: golden {want['content_type']!r} != "
                     f"replayed {got['content_type']!r}")
    if got["body"] != want_body:
        i = next((n for n, (a, b) in enumerate(zip(want_body, got["body"])) if a != b),
                 min(len(want_body), len(got["body"])))
        diffs.append(f"body: first byte drift at offset {i} "
                     f"(golden {len(want_body)}B, replayed {len(got['body'])}B); "
                     f"golden[{i}:{i + 40}]={want_body[i:i + 40]!r} "
                     f"replayed={got['body'][i:i + 40]!r}")
    probe = _probe(agent_url, engine, timeout)
    if probe != want_probe:
        diffs.append(f"probe: golden {want_probe} != replayed {probe}")
    return diffs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", required=True, help="vision | embed | tabular")
    ap.add_argument("--replay", action="store_true",
                    help="replay + byte-diff against the recorded golden (the swap gate)")
    ap.add_argument("--agent-url", default=os.getenv("AGENT_URL", "http://127.0.0.1:8100"))
    ap.add_argument("--goldens-root", default=GOLDENS_ROOT)
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="per-request timeout (cold loads pull model weights)")
    args = ap.parse_args(argv)

    if not args.replay:
        outdir = capture(args.engine, args.agent_url.rstrip("/"), args.goldens_root, args.timeout)
        print(f"golden captured: {outdir}")
        return 0
    diffs = replay(args.engine, args.agent_url.rstrip("/"), args.goldens_root, args.timeout)
    if diffs:
        print(f"GOLDEN GATE FAIL ({args.engine}):", file=sys.stderr)
        for d in diffs:
            print(f"  - {d}", file=sys.stderr)
        return 1
    print(f"golden gate PASS ({args.engine}): byte-identical at the agent boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
