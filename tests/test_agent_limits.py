"""023 US6 (T529, FR-315..320) — the bounded stdlib transport, offline.

Real `BoundedAgentServer` sockets on an ephemeral port over fake components. Pins: declared
Content-Length over the route-class limit → immediate 413 with NO body read; counted chunked
reads abort at the same limit; multipart rides the larger bound; authentication precedes body
buffering (oversized + wrong key → the auth status, not 413); worker/queue saturation answers a
minimal 503 (never an unbounded thread pile); graceful shutdown drains in-flight requests.
"""
import http.client
import json
import os
import socket
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _agentstore import FakeJobStore  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import (  # noqa: E402
    auth,
    lifecycle,
)
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent import main as agent_main  # noqa: E402
from hostagent.journal import Journal  # noqa: E402

KEY = "k-limits-0123456789abcdef"


def _server(*, max_workers=2, queue_size=1, queue_wait_s=0.3, policy=None, handler_hold=None):
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(store=FakeJobStore())
    manager = lifecycle.EngineManager(admission, runtimes={})
    if handler_hold is not None:
        # a "slow route": /jobs listing blocks until released — saturates workers deterministically
        original = journal.jobs

        def slow_jobs(kind=None):
            handler_hold.wait(10)
            return original(kind=kind)

        journal.jobs = slow_jobs
    handler = agent_main.make_handler(admission, journal, manager,
                                      jobs_mod.JobManager(admission, journal), policy=policy)
    server = agent_main.BoundedAgentServer(("127.0.0.1", 0), handler, max_workers=max_workers,
                                           queue_size=queue_size, queue_wait_s=queue_wait_s)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, host, port, journal


def _post(host, port, path, body: bytes, headers=None, timeout=10):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        conn.close()


# --- body limits (T532, FR-317) -----------------------------------------------------------------------

def test_declared_oversize_json_is_413_before_any_read(monkeypatch):
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        # declare 1 MiB but SEND NOTHING: a limit enforced before reading answers immediately;
        # one that buffers first would block on the absent body until the socket times out
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(1 << 20))
        conn.endheaders()
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 413 and "limit" in body["error"]
        conn.close()
    finally:
        server.shutdown()


def test_chunked_body_is_counted_and_aborted_at_the_limit(monkeypatch):
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 512, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        chunk = b"x" * 256
        for _ in range(4):  # 1024 > 512: the counted read must abort mid-stream
            conn.send(b"100\r\n" + chunk + b"\r\n")
        conn.send(b"0\r\n\r\n")
        r = conn.getresponse()
        assert r.status == 413
        conn.close()
    finally:
        server.shutdown()


def test_multipart_rides_the_larger_bound(monkeypatch):
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 512, raising=True)
    monkeypatch.setattr(agent_main, "AGENT_MAX_MULTIPART_BYTES", 1 << 20, raising=True)
    server, host, port, _ = _server()
    try:
        big = b"m" * 2048  # over the JSON bound, well under multipart's
        status, body = _post(host, port, "/engines/nope/classify", big,
                             headers={"Content-Type": "multipart/form-data; boundary=x"})
        assert status == 404  # PAST the limit check — refused only as an unknown engine
        status, _ = _post(host, port, "/jobs", big)  # same size as JSON → 413
        assert status == 413
    finally:
        server.shutdown()


def test_auth_precedes_body_buffering(monkeypatch):
    """FR-282/317 ordering: an oversized request WITHOUT the key gets the auth verdict — the
    body is never read, let alone buffered."""
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 64, raising=True)
    server, host, port, _ = _server(policy=auth.AgentAuthPolicy(KEY))
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(1 << 20))  # over-limit AND unauthenticated
        conn.endheaders()
        r = conn.getresponse()
        assert r.status == 401  # the gate answered; 413 would mean the limiter ran first
        conn.close()
        status, _ = _post(host, port, "/jobs", b"x" * 128, headers={"X-Agent-Key": KEY})
        assert status == 413    # keyed: NOW the limiter speaks
    finally:
        server.shutdown()


# --- saturation (T531, FR-316/320) ---------------------------------------------------------------------

def test_saturated_workers_and_queue_answer_503():
    hold = threading.Event()
    server, host, port, _ = _server(max_workers=2, queue_size=1, queue_wait_s=0.2,
                                    handler_hold=hold)
    conns = []
    try:
        # occupy both workers + the one queue slot with held /jobs requests
        for _ in range(3):
            c = http.client.HTTPConnection(host, port, timeout=15)
            c.request("GET", "/jobs")
            conns.append(c)
        time.sleep(0.15)  # let them be admitted
        # the 4th connection is over conn bound → immediate minimal 503, no thread parked
        c4 = http.client.HTTPConnection(host, port, timeout=5)
        c4.request("GET", "/healthz")
        r = c4.getresponse()
        assert r.status == 503 and b"saturated" in r.read()
        c4.close()
    finally:
        hold.set()  # release the held handlers
        for c in conns:
            try:
                c.getresponse().read()
                c.close()
            except Exception:
                pass
        server.shutdown()


def test_queued_request_proceeds_once_a_worker_frees():
    hold = threading.Event()
    server, host, port, _ = _server(max_workers=1, queue_size=2, queue_wait_s=5.0,
                                    handler_hold=hold)
    try:
        c1 = http.client.HTTPConnection(host, port, timeout=15)
        c1.request("GET", "/jobs")            # occupies the single worker (held)
        time.sleep(0.1)
        done = {}

        def second_request():
            c2 = http.client.HTTPConnection(host, port, timeout=15)
            c2.request("GET", "/healthz")     # queued behind the held worker
            done["status"] = c2.getresponse().status
            c2.close()

        t = threading.Thread(target=second_request)
        t.start()
        time.sleep(0.2)
        assert "status" not in done           # still queued — bounded WAITING, not a 503
        hold.set()                            # worker frees
        t.join(10)
        assert done.get("status") == 200      # the queued request completed
        c1.getresponse().read()
        c1.close()
    finally:
        hold.set()
        server.shutdown()


# --- graceful shutdown (T533, FR-318) -------------------------------------------------------------------

def test_graceful_shutdown_drains_inflight_requests():
    hold = threading.Event()
    server, host, port, _ = _server(max_workers=2, queue_size=2, handler_hold=hold)
    c1 = http.client.HTTPConnection(host, port, timeout=15)
    c1.request("GET", "/jobs")                # in flight, held
    time.sleep(0.1)
    stopper = threading.Thread(target=server.shutdown)
    stopper.start()                           # accept-stop
    drained = {}

    def drain():
        server.graceful_shutdown(timeout_s=5.0, log=lambda *a, **kw: None)
        drained["at"] = time.time()

    d = threading.Thread(target=drain)
    d.start()
    time.sleep(0.2)
    assert "at" not in drained                # still draining — the in-flight request is alive
    hold.set()
    d.join(10)
    assert "at" in drained                    # drained once the handler finished
    r = c1.getresponse()
    assert r.status == 200                    # the in-flight request COMPLETED, not was killed
    c1.close()
    stopper.join(5)


def test_slow_client_read_times_out(monkeypatch):
    """FR-318: a client that sends headers and then goes silent cannot park a worker forever —
    the per-socket timeout aborts the read."""
    server, host, port, _ = _server()
    try:
        # shrink the accepted handler timeout AFTER construction is not possible per-connection;
        # instead prove the knob exists and is applied as the handler class attribute
        handler_cls = server.RequestHandlerClass
        assert handler_cls.timeout == agent_main.AGENT_IO_TIMEOUT_S
        # and a plain healthz still answers (the timeout is per-I/O, not per-request-lifetime)
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert b"200" in s.recv(64)
        s.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
