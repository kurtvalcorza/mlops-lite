"""020 T414 — scripts/agent_stream_drill.py offline smoke (fake SSE agent).

A stdlib HTTP server plays the agent: an SSE stream with controllable inter-frame delays, a
/health endpoint, a vision classify, and a preempt-target predict. Pins: TTFT/stall counting
against the frame timing, the disconnect scenario (cut after 2 frames → next stream clean), the
RuntimeBaselineRecord shape + baseline miss logic, and the runbook append.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if os.path.join(REPO, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "scripts"))

import agent_stream_drill as drill  # noqa: E402


class FakeAgent(BaseHTTPRequestHandler):
    frame_delays = [0.0, 0.05, 0.05]   # per-frame sleeps; a test can inject a stall

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if self.path.startswith("/engines/llm/infer/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for i, delay in enumerate(FakeAgent.frame_delays):
                    time.sleep(delay)
                    self.wfile.write(f'data: {{"event": "t{i}"}}\n\n'.encode())
                    self.wfile.flush()
            except OSError:
                pass  # client cut mid-stream (the disconnect scenario)
        elif self.path.startswith("/engines/vision/classify"):
            body = b'{"model": "m", "predictions": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/engines/tabular/predict"):
            body = b'{"error": "held"}'
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def agent():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeAgent.frame_delays = [0.0, 0.05, 0.05]
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


BODY = {"prompt": "hi", "max_tokens": 4, "temperature": 0.1}


def test_measure_stream_counts_frames_and_stalls(agent):
    out = drill.measure_stream(agent, "llm", "infer", BODY, runs=2, stall_gap_s=1.0,
                               poll_hz=20.0, timeout=10)
    assert out["frames_median"] == 3 and out["stalls"] == 0
    assert out["ttft_ms"] < 1000
    assert out["health_polls"] > 0 and out["health_poll_failures"] == 0


def test_stall_detected_when_a_frame_gap_exceeds_threshold(agent):
    FakeAgent.frame_delays = [0.0, 0.35, 0.02]   # one gap over the 0.2s threshold
    out = drill.measure_stream(agent, "llm", "infer", BODY, runs=1, stall_gap_s=0.2,
                               poll_hz=20.0, timeout=10)
    assert out["stalls"] == 1


def test_disconnect_scenario_recovers(agent):
    out = drill.measure_disconnect(agent, "llm", "infer", BODY, stall_gap_s=1.0, timeout=10)
    assert out["disconnect_ok"] is True
    assert out["next_request_ttft_ms"] > 0


def test_preempt_during_stream_records_behavior(agent):
    out = drill.measure_preempt_during_stream(agent, "llm", "infer", BODY,
                                              target_engine="tabular", stall_gap_s=1.0,
                                              timeout=10)
    assert out["preempt_status"] == 409 and out["behavior"] == "refused-409"
    assert out["stream_completed_frames"] == 3


def test_record_shape_and_baseline_misses(agent, tmp_path):
    stream = drill.measure_stream(agent, "llm", "infer", BODY, runs=1, stall_gap_s=1.0,
                                  poll_hz=20.0, timeout=10)
    disconnect = drill.measure_disconnect(agent, "llm", "infer", BODY, stall_gap_s=1.0,
                                          timeout=10)
    baselines = {"ttft_ms": 0.001, "stalls_max": 0, "multipart_ms": None, "stall_gap_s": 1.0}
    record = drill.build_record("stdlib", stream, None, disconnect, None, baselines)
    # RuntimeBaselineRecord fields (data-model.md)
    for field in ("runtime", "ttft_ms", "stalls", "multipart_ms", "disconnect_ok",
                  "swap_contention", "baselines", "misses", "meets_baselines"):
        assert field in record, field
    assert record["meets_baselines"] is False           # the absurd 0.001ms baseline must miss
    assert any("ttft_ms" in m for m in record["misses"])

    runbook = tmp_path / "runbook.md"
    runbook.write_text("# runbook\n")
    drill.append_runbook(record, str(runbook))
    text = runbook.read_text()
    assert "RuntimeBaselineRecord" in text and '"runtime": "stdlib"' in text


def test_cli_end_to_end_json_record(agent, capsys):
    rc = drill.main(["--agent-url", agent, "--runtime", "stdlib", "--runs", "1",
                     "--skip-multipart", "--skip-preempt", "--stall-gap-s", "1.0",
                     "--timeout", "10"])
    assert rc == 0
    record = json.loads(capsys.readouterr().out)
    assert record["runtime"] == "stdlib" and record["disconnect_ok"] is True
    assert record["meets_baselines"] is True            # no baselines given => nothing to miss
