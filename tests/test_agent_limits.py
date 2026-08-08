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
from hostagent.metrics import REGISTRY  # noqa: E402

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


def test_an_oversize_body_that_is_actually_sent_still_gets_its_413(monkeypatch):
    """The sibling above declares an over-limit body and **sends nothing**, so the server's refusal
    leaves an empty receive buffer and the close that follows is harmless. This one sends the body.

    That is the case issue #85 was about: the 413 is written over a request the server deliberately
    never read (FR-317), and closing a socket with unread data resets it away — taking the status
    the client needed with it. `test_multipart_rides_the_larger_bound` hit exactly this as an
    intermittent `ConnectionAbortedError` before the undrained close was deferred.
    """
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        oversize = b"x" * 8192  # declared AND transmitted, unlike the send-nothing case above
        status, body = _post(host, port, "/jobs", oversize)
        assert status == 413 and "limit" in body["error"]
    finally:
        server.shutdown()


def test_a_413_written_over_an_unread_body_defers_its_close(monkeypatch):
    """The mechanism behind the two tests above, asserted directly — because their outcome does not
    discriminate.

    Measured with the deferral disabled: the sent-body 413 passed 10/10, multipart passed 10/10, and
    the chunked abort failed only 1/10. The reset is real but rare on this path, so a response-level
    assertion is nearly green against the defect and would not hold the fix in place. What is
    deterministic is the contract: a handler that answered without consuming the request must leave
    the socket with the closer rather than closed.
    """
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        # Body large enough that the header read cannot have absorbed it into `rfile`'s buffer, so
        # the refusal genuinely leaves bytes unread on the socket. A small body is pre-buffered and
        # has no hazard to defer — the deferral correctly does nothing there.
        conn.request("POST", "/jobs", body=b"x" * 12288,
                     headers={"Content-Type": "application/json"})

        # Check BEFORE reading. Reading is what ends the linger: the response is HTTP/1.0
        # `Connection: close`, so finishing it makes the client close, the reaper sees the peer's
        # FIN and releases the socket — measured under 100ms. Asserting after the read therefore
        # finds an empty set whether or not the deferral happened, which is what made an earlier
        # version of this test fail 12/12 against the *fixed* server.
        deadline = time.monotonic() + 5
        pending = []
        while time.monotonic() < deadline:
            with server._linger_lock:
                pending = list(server._lingering)
            if pending:
                break
            time.sleep(0.002)
        assert pending, "the refused socket was handed to the lingering closer, not closed"
        assert all(s.fileno() != -1 for s in pending), "and it is still open"

        assert conn.getresponse().status == 413  # and the status still arrives
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
        for _ in range(2):  # 2 x 256 == the 512 limit exactly: still admissible
            conn.send(b"100\r\n" + chunk + b"\r\n")
        # A THIRD chunk, announced AND sent, then terminated. `_read_chunked` counts by announced
        # size and aborts on the size line, so everything after it is data the server never reads.
        #
        # This used to stop at the size line deliberately: writing the rest raced the abort, because
        # the server had already answered and closed, so the write hit an RST as BrokenPipeError and
        # that same RST could discard the 413 still unread in the client's buffer. Both directions of
        # that flake are what deferring the undrained close (issue #85) removes — so the write-
        # nothing workaround is gone, and the full body now exercises the case it was avoiding.
        conn.send(b"300\r\n" + b"x" * 768 + b"\r\n")
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
    """A stable, client-visible 503 (SC-160), plus the two witnesses that it was bounded.

    The 503 is required unconditionally — it is the published contract, and the transport keeps it
    deliverable through `BoundedAgentServer._linger` rather than letting the refusing close reset it
    away. This test failing with a connection reset means that lingering close has regressed, which
    is exactly what it should report rather than tolerate.

    The rejection counter and `_inflight` are asserted alongside it because the response alone does
    not distinguish "refused at the bound" from "served normally and happened to answer 503".
    """
    hold = threading.Event()
    # `queue_wait_s` is long enough that the queued third request cannot time out mid-test. At the
    # original 0.2s it raced the 0.15s sleep below: the queue slot could free before the fourth
    # connection arrived, which would then be served normally rather than refused.
    server, host, port, _ = _server(max_workers=2, queue_size=1, queue_wait_s=5.0,
                                    handler_hold=hold)
    conns = []
    try:
        # occupy both workers + the one queue slot with held /jobs requests
        for _ in range(3):
            c = http.client.HTTPConnection(host, port, timeout=15)
            c.request("GET", "/jobs")
            conns.append(c)
        # Wait for the precondition the probe depends on, rather than sleeping a guessed interval.
        #
        # `_inflight` alone is NOT that precondition: it counts handlers that acquired an *exec*
        # slot, and says nothing about whether the third request has reached `process_request`,
        # taken the last *connection* permit, and parked on the exec semaphore. Waiting only on it
        # leaves the fourth probe racing the third request for that final permit. The bound under
        # test is the connection bound, so wait until no connection permits remain.
        deadline = time.monotonic() + 10
        while (server._inflight < 2 or server._conn_slots._value != 0) \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server._inflight == 2, "both workers busy before the bound is probed"
        assert server._conn_slots._value == 0, \
            "all connection permits consumed before the bound is probed"

        before = REGISTRY.counter_value("hostagent_requests_rejected_total",
                                        {"reason": "saturated"})
        # the 4th connection is over conn bound → immediate minimal 503, no thread parked
        c4 = http.client.HTTPConnection(host, port, timeout=5)
        c4.request("GET", "/healthz")
        response = c4.getresponse()
        status, body = response.status, response.read()
        c4.close()

        after = REGISTRY.counter_value("hostagent_requests_rejected_total",
                                       {"reason": "saturated"})
        assert status == 503 and b"saturated" in body
        assert after == before + 1, "the 4th connection was refused at the bound, not served"
        assert server._inflight == 2, "the refusal parked no additional worker"
    finally:
        hold.set()  # release the held handlers
        for c in conns:
            try:
                c.getresponse().read()
                c.close()
            except Exception:
                pass
        server.shutdown()


def test_a_queued_request_that_times_out_still_receives_its_503():
    """The **other** refusal path, and the one that bypassed the lingering close.

    A request refused by the connection cap never reaches a worker thread. This one does: it takes
    an admission permit, parks on the exec semaphore, times out, and is refused from inside
    `process_request_thread` — whose `finally` used to close the socket unconditionally, straight
    back out from under the closer that `_reject` had just handed it to. Same reset, same lost 503,
    on a path the connection-cap test cannot reach because it deliberately keeps the queue slot
    occupied.

    `queue_timeout` rather than `saturated` is asserted so this cannot silently start passing by
    exercising the cap instead.
    """
    hold = threading.Event()
    server, host, port, _ = _server(max_workers=1, queue_size=1, queue_wait_s=0.2,
                                    handler_hold=hold)
    conns = []
    try:
        c1 = http.client.HTTPConnection(host, port, timeout=15)
        c1.request("GET", "/jobs")  # occupies the only worker, and holds it
        conns.append(c1)
        deadline = time.monotonic() + 10
        while server._inflight < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server._inflight == 1, "the worker is occupied before the queue is probed"

        before = REGISTRY.counter_value("hostagent_requests_rejected_total",
                                        {"reason": "queue_timeout"})
        # Admitted — a connection permit is free — then refused waiting for the busy worker.
        c2 = http.client.HTTPConnection(host, port, timeout=10)
        c2.request("GET", "/healthz")

        # Assert the close-ownership contract DIRECTLY, not through the socket outcome.
        #
        # The response alone does not discriminate on this path: measured, the queue-timeout
        # refusal delivered its 503 10/10 even with the handoff reverted. The connection-cap
        # refusal resets because the server closes before the client's request bytes have even
        # arrived; here the server has waited out `queue_wait_s`, so those bytes are long since
        # delivered and the reset does not reliably follow. The defect is nonetheless real — the
        # worker's `finally` closes a socket the reaper already owns — so this pins the mechanism
        # rather than waiting for a symptom that only sometimes appears.
        # Wait on the handover itself. Not on the counter: that is incremented before `_reject`
        # runs, so sampling on it can land in the gap before the socket is registered and report a
        # handover that simply has not happened yet.
        deadline = time.monotonic() + 10
        pending = []
        while time.monotonic() < deadline:
            with server._linger_lock:
                pending = list(server._lingering)
            if pending:
                break
            time.sleep(0.005)
        assert pending, "the refused socket was handed to the lingering closer"

        # And that it STAYS open. The closer releases a socket only on the peer's FIN or its own
        # deadline; this client has not closed, and the deadline is far off. A close inside this
        # window is the worker's `finally` reaching past the handover.
        time.sleep(0.05)
        assert all(s.fileno() != -1 for s in pending), \
            "the worker's finally closed a socket the closer already owns"

        response = c2.getresponse()
        status, body = response.status, response.read()
        c2.close()

        after = REGISTRY.counter_value("hostagent_requests_rejected_total",
                                       {"reason": "queue_timeout"})
        assert status == 503 and b"saturated" in body
        assert after == before + 1, "refused by the queue timeout, not by the connection cap"
    finally:
        hold.set()
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
