"""020 T409 — scripts/capture_goldens.py (the US2 byte-parity swap gate, offline).

A fake agent (stdlib HTTP server on a loopback port) stands in for the real agent boundary:
canned canonical responses per engine route + a stable `/engines/<e>/health`. Pins the
capture→replay round-trip (clean gate), drift detection on a 1-byte response change, a
content-type drift, a probe drift, and the golden-set file layout (data-model.md GoldenSet).

The vision leg (PIL fixture image + multipart) is exercised too — PIL is a host-venv dep.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if os.path.join(REPO, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "scripts"))

import capture_goldens as cg  # noqa: E402


class FakeAgent(BaseHTTPRequestHandler):
    """Byte-stable canned agent: verb responses keyed by path; health returns ok=True."""

    responses = {}   # path -> (status, content_type, body_bytes) — class-level, set per fixture
    seen = []        # (path, content_type, body) of every POST

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeAgent.seen.append((self.path, self.headers.get("Content-Type", ""), body))
        status, ctype, payload = FakeAgent.responses.get(
            self.path, (404, "application/json", b'{"detail":"no verb"}'))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/engines/") and self.path.endswith("/health"):
            payload = json.dumps({"ok": True, "resident": False, "vram_free_gb": 11.3}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # keep pytest output clean
        pass


@pytest.fixture
def agent():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeAgent.responses = {
        "/engines/embed/embed": (200, "application/json", b"[[0.1,0.2],[0.3,0.4]]"),
        "/engines/tabular/predict": (
            200, "application/json",
            b'{"model":"tabular-lgbm","device":"cpu","features":["f0"],'
            b'"predictions":[{"prediction":1,"score":0.9}]}'),
        "/engines/vision/classify": (
            200, "application/json",
            b'{"model":"vision-mobilenet","device":"cuda",'
            b'"predictions":[{"label":"tabby cat","score":0.91}]}'),
    }
    FakeAgent.seen = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_capture_then_replay_round_trip_passes(agent, tmp_path):
    root = str(tmp_path / "goldens")
    outdir = cg.capture("embed", agent, root, timeout=5)
    # golden layout (data-model.md GoldenSet)
    for name in ("request.json", "request.body", "response.json", "response.body", "probe.json"):
        assert os.path.isfile(os.path.join(outdir, name)), name
    assert json.loads(open(os.path.join(outdir, "probe.json")).read()) == {"status": 200,
                                                                           "ok": True}
    # the recorded request is the canonical one
    assert json.loads(open(os.path.join(outdir, "request.body"), "rb").read().decode()) == \
        {"texts": cg.EMBED_TEXTS}
    assert cg.replay("embed", agent, root, timeout=5) == []


def test_replay_detects_one_byte_body_drift(agent, tmp_path):
    root = str(tmp_path / "goldens")
    cg.capture("tabular", agent, root, timeout=5)
    status, ctype, body = FakeAgent.responses["/engines/tabular/predict"]
    drifted = body.replace(b'"score":0.9', b'"score":0.8')  # a single-digit (1-byte) drift
    FakeAgent.responses["/engines/tabular/predict"] = (status, ctype, drifted)
    diffs = cg.replay("tabular", agent, root, timeout=5)
    assert len(diffs) == 1 and diffs[0].startswith("body:") and "offset" in diffs[0]


def test_replay_detects_content_type_and_status_drift(agent, tmp_path):
    root = str(tmp_path / "goldens")
    cg.capture("embed", agent, root, timeout=5)
    status, _, body = FakeAgent.responses["/engines/embed/embed"]
    FakeAgent.responses["/engines/embed/embed"] = (500, "text/plain", body)
    diffs = cg.replay("embed", agent, root, timeout=5)
    kinds = {d.split(":")[0] for d in diffs}
    assert "status" in kinds and "content-type" in kinds


def test_vision_multipart_capture_uses_fixed_boundary_and_replays(agent, tmp_path):
    root = str(tmp_path / "goldens")
    cg.capture("vision", agent, root, timeout=5)
    sent = [s for s in FakeAgent.seen if s[0] == "/engines/vision/classify"]
    assert sent, "no classify request reached the agent"
    _, ctype, body = sent[0]
    assert cg.BOUNDARY in ctype and b'name="image"' in body and b"PNG" in body[:200]
    assert cg.replay("vision", agent, root, timeout=5) == []
    # replay sent byte-identical request bytes (the fixed boundary makes this deterministic)
    replay_sent = [s for s in FakeAgent.seen if s[0] == "/engines/vision/classify"]
    assert replay_sent[0][2] == replay_sent[-1][2]


def test_capture_refuses_unhealthy_child(agent, tmp_path):
    FakeAgent.responses["/engines/embed/embed"] = (503, "application/json", b'{"detail":"cold"}')
    with pytest.raises(SystemExit, match="HEALTHY"):
        cg.capture("embed", agent, str(tmp_path / "goldens"), timeout=5)


def test_replay_without_golden_fails_loud(agent, tmp_path):
    with pytest.raises(SystemExit, match="capture first"):
        cg.replay("embed", agent, str(tmp_path / "goldens"), timeout=5)
