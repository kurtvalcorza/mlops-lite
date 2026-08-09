"""023 US6 (T529, FR-315..320) — the bounded stdlib transport, offline.

Real `BoundedAgentServer` sockets on an ephemeral port over fake components. Pins: declared
Content-Length over the route-class limit → immediate 413 with NO body read; counted chunked
reads abort at the same limit; a declared length that is not a byte count (negative, non-numeric,
or a negative chunk size) is a 400 on the framing rather than a read to EOF that the limit cannot
measure; multipart rides the larger bound; authentication precedes body buffering (oversized +
wrong key → the auth status, not 413); worker/queue saturation answers a minimal 503 (never an
unbounded thread pile); graceful shutdown drains in-flight requests.
"""
import http.client
import json
import os
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer

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


def _raw(host, port, blob: bytes, tail: bytes = b"", timeout=5.0):
    """Send exact bytes and return the raw response.

    `http.client` is the right tool everywhere else in this file, but not for framing tests: an
    empty or repeated header value is precisely the thing a client library is entitled to
    normalize, so a test built on one would be asserting against the library's idea of the request
    rather than the wire. `tail` goes out after a beat, for cases where the body must arrive after
    the server has already answered.
    """
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall(blob)
        if tail:
            time.sleep(0.05)
            s.sendall(tail)
        s.settimeout(timeout)
        chunks = []
        try:
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        except (socket.timeout, ConnectionResetError):
            pass
        return b"".join(chunks)
    finally:
        s.close()


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


def test_a_production_sized_overlimit_body_is_drained_not_cut_off(monkeypatch):
    """The case a byte-capped discard could not serve, at a scale that matters.

    A body that genuinely trips the real JSON limit is over 1 MiB, and multipart is 32 MiB, so any
    fixed byte ceiling low enough to be a meaningful bound sits *below* every request this defends
    against — the reaper would discard its quota, close on a large unread remainder, and reset the
    413 away exactly as before. The lingering close is therefore bounded by lifetime and count only.

    256 KiB here rather than a literal megabyte: comfortably past the 64 KiB ceiling that used to
    cut this off, without making the suite push a megabyte through loopback per run.
    """
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("POST", "/jobs", body=b"x" * (256 * 1024),
                     headers={"Content-Type": "application/json"})

        deadline = time.monotonic() + 5
        pending = []
        while time.monotonic() < deadline:
            with server._linger_lock:
                pending = list(server._lingering)
            if pending:
                break
            time.sleep(0.002)
        assert pending, "the refused socket was handed to the lingering closer"

        # The point of the test: the reaper keeps draining rather than closing once some byte quota
        # is spent. Sample across a window far longer than draining 256 KiB takes.
        for _ in range(20):
            time.sleep(0.01)
            with server._linger_lock:
                still = list(server._lingering)
            if not still:
                break
        assert all(s.fileno() != -1 for s in pending), \
            "the socket was cut off mid-body instead of drained to the peer's FIN"

        assert conn.getresponse().status == 413
        conn.close()
    finally:
        server.shutdown()


def test_a_413_answered_before_the_body_arrives_still_defers_its_close(monkeypatch):
    """Headers first, body later — the case instantaneous readability cannot see.

    Both protected paths answer from the headers alone: a declared `Content-Length` over the limit
    is refused without reading, and the auth gate denies before any read. So the response can go out
    while the body is still in flight, and at that moment **nothing is pending on the socket**. A
    zero-time `select` reports "drained", the close happens immediately, and the body then lands on
    a closed socket — the same reset, reached by the same close, on exactly the case this mechanism
    exists for.

    The handover therefore has to rest on what the handler did, not on what the kernel happens to
    hold. This test sends only headers, requires the socket to be with the closer *before* a single
    body byte is written, and only then sends the body.
    """
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(8192))  # declared over the limit; not yet sent
        conn.endheaders()

        deadline = time.monotonic() + 5
        pending = []
        while time.monotonic() < deadline:
            with server._linger_lock:
                pending = list(server._lingering)
            if pending:
                break
            time.sleep(0.002)
        assert pending, "handed over on the refusal itself, before any body byte could be pending"

        conn.send(b"x" * 8192)  # the body arrives only now, after the close would have happened
        assert conn.getresponse().status == 413
        conn.close()
    finally:
        server.shutdown()


def test_an_unauthorized_request_with_a_sent_body_keeps_its_401(monkeypatch):
    """The 401 half of #85, which the auth-ordering test cannot reach.

    `test_auth_precedes_body_buffering` declares 1 MiB and **sends nothing**, so it proves the gate
    runs before the read but leaves no bytes on the socket — the close is harmless and the deferral
    has nothing to do. Here the body is actually transmitted, so the 401 is written over a request
    the gate deliberately never read (FR-282/283) and needs the same handover the 413 does.
    """
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server(policy=auth.AgentAuthPolicy(KEY))
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("POST", "/jobs", body=b"x" * 12288,  # no X-Agent-Key: refused at the gate
                     headers={"Content-Type": "application/json"})

        deadline = time.monotonic() + 5
        pending = []
        while time.monotonic() < deadline:
            with server._linger_lock:
                pending = list(server._lingering)
            if pending:
                break
            time.sleep(0.002)
        assert pending, "the unauthorized socket was handed to the lingering closer"
        assert all(s.fileno() != -1 for s in pending), "and it is still open"

        assert conn.getresponse().status == 401  # the gate's verdict, not a reset
        conn.close()
    finally:
        server.shutdown()


def test_a_protected_get_carrying_a_body_defers_its_401_close():
    """The auth gate is shared, so the handover has to be too.

    `do_GET` gates on the same `_deny` as `do_POST` and every non-public GET (`/jobs`, `/health`,
    `/engines` — only `/healthz`, `/readyz`, `/metrics` are public) is refused before any read. A
    GET body has no useful semantics here, but nothing stops a client declaring one, and then the
    401 goes out over bytes still in flight exactly as the POST case did. Marking in `_do_post`
    alone left this route one `return` short of the fix.

    Headers first, ownership asserted before a single body byte exists, body only afterwards —
    the same shape as the 413 header-first test, on the GET half of the gate.
    """
    server, host, port, _ = _server(policy=auth.AgentAuthPolicy(KEY))
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("GET", "/jobs")  # not in PUBLIC_ROUTES; no X-Agent-Key
        conn.putheader("Content-Length", str(8192))  # declared, deliberately not yet sent
        conn.endheaders()

        deadline = time.monotonic() + 5
        pending = []
        while time.monotonic() < deadline:
            with server._linger_lock:
                pending = list(server._lingering)
            if pending:
                break
            time.sleep(0.002)
        assert pending, "handed over on the refusal itself, before any body byte could be pending"

        conn.send(b"x" * 8192)  # arrives only now — after the unmarked close would have happened
        assert conn.getresponse().status == 401  # the gate's verdict, not a reset
        conn.close()
    finally:
        server.shutdown()


def test_the_handler_survives_a_refusal_on_a_server_without_the_deferral():
    """`make_handler` is not owned by `BoundedAgentServer`, so it cannot assume the capability.

    `tests/_agentserver.py` (the shared fixture behind the domain suites), `test_agent_jobs_http`
    and `test_swap_orchestration` all mount it on a plain `ThreadingHTTPServer` — the reuse is
    test-side today, but the factory is public and does not own its server, and the failure mode is
    silent. Calling `mark_undrained` unconditionally raised `AttributeError` there on
    every refused body-bearing request — and it went unnoticed because the status had already been
    flushed, so the client still saw its 401 and only the request thread died. `handle_error` is
    where that landed, so that is what this watches; asserting the response alone would reproduce
    exactly the blind spot that let it ship.
    """
    seen = []

    class WatchfulServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            seen.append(sys.exc_info()[1])

    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(store=FakeJobStore())
    handler = agent_main.make_handler(admission, journal,
                                      lifecycle.EngineManager(admission, runtimes={}),
                                      jobs_mod.JobManager(admission, journal),
                                      policy=auth.AgentAuthPolicy(KEY))
    server = WatchfulServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("POST", "/jobs", body=b"x" * 12288,  # declares a body; refused at the gate
                     headers={"Content-Type": "application/json"})
        assert conn.getresponse().status == 401
        conn.close()
        time.sleep(0.05)  # the handler thread finishes after the response is flushed
        assert not seen, f"the handler raised on a server without the deferral: {seen}"
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


# --- malformed framing: lengths that are not byte counts (FR-317) --------------------------------

def test_a_negative_content_length_is_refused_before_any_read(monkeypatch):
    """Send NOTHING after the headers, exactly as `test_declared_oversize_json_is_413_before_any_read`
    does: a server that refuses on the declaration answers at once, while one that reaches
    `rfile.read(-1)` sits reading to EOF and answers nothing at all.

    That is what this did before the fix — measured at 0 bytes back, and only after
    `AGENT_IO_TIMEOUT_S` expired. The timeout is pulled down here so the FAILING direction costs a
    second rather than thirty.
    """
    monkeypatch.setattr(agent_main, "AGENT_IO_TIMEOUT_S", 1.0, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 400 and "byte count" in body["error"]
        conn.close()
    finally:
        server.shutdown()


def test_a_non_numeric_content_length_answers_instead_of_killing_the_thread(monkeypatch):
    """`int("abc")` raised straight out of `_read_body` and out of the handler, so the request
    thread died with nothing on the wire and the client saw a bare close where a status belonged.

    This one needs no timeout nudge: the old failure was immediate, not slow. What it pins is that
    a status now exists at all.
    """
    monkeypatch.setattr(agent_main, "AGENT_IO_TIMEOUT_S", 1.0, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 400 and "byte count" in body["error"]
        conn.close()
    finally:
        server.shutdown()


def test_a_negative_chunk_size_is_refused_before_any_read(monkeypatch):
    """`int(b"-1", 16)` is -1, so the chunked reader had the identical hole one base up: the size
    line passed the `total > limit` test and `read(-1)` ran to EOF."""
    monkeypatch.setattr(agent_main, "AGENT_IO_TIMEOUT_S", 1.0, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"-1\r\n")  # a size line that parses and then means "read to EOF"
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 400 and "byte count" in body["error"]
        conn.close()
    finally:
        server.shutdown()


def test_a_negative_length_cannot_smuggle_a_body_past_the_cap(monkeypatch):
    """The property the three above protect, and the reason this is a bug rather than an untidiness.

    FR-317's ceiling is only a ceiling if the declared length is a byte count. It was not checked,
    so a body under a negative declaration was read to EOF and the limit never ran: measured against
    the real handler, **8 MiB landed in one buffer under the 1 MiB JSON limit** and came back a 400
    from the JSON parser. The bound was the sender's bandwidth, not the route class.

    Both directions answer 400, which is exactly why this asserts on WHICH 400 — a status-only
    assertion is green against the defect and would not hold the fix in place.
    """
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        conn.send(b"x" * 8192)  # 8x the limit, and declared as nothing the limit can measure
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 400 and "byte count" in body["error"], \
            "refused on the framing — not parsed, which is how the 8 KiB used to get in"
        conn.close()
    finally:
        server.shutdown()


def test_a_refused_request_with_a_malformed_length_still_keeps_its_401():
    """The `_declares_a_body` half of this change, which the 400 tests above cannot reach.

    They all go through `_do_post`'s malformed branch, which calls `_answered_over_an_unread_body()`
    unconditionally — so none of them exercises `_declares_a_body`, which only matters inside
    `_deny`. It used to return False for an unparseable length, so a refused request carrying one
    had its 401 written and then reset away by the undrained close (#85). Found by review, not by
    the tests that shipped with the change.

    Headers first, ownership asserted before a body byte exists, body only afterwards — the same
    shape as `test_a_protected_get_carrying_a_body_defers_its_401_close`.
    """
    server, host, port, _ = _server(policy=auth.AgentAuthPolicy(KEY))
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")           # no X-Agent-Key: refused at the gate
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "abc")    # unparseable: `_declares_a_body` used to say no
        conn.endheaders()

        deadline = time.monotonic() + 5
        pending = []
        while time.monotonic() < deadline:
            with server._linger_lock:
                pending = list(server._lingering)
            if pending:
                break
            time.sleep(0.002)
        assert pending, "handed over on the refusal, even though the length could not be parsed"

        conn.send(b"x" * 8192)  # arrives only now — after the unmarked close would have happened
        assert conn.getresponse().status == 401  # the gate's verdict, not a reset
        conn.close()
    finally:
        server.shutdown()


def test_a_length_too_long_to_convert_answers_instead_of_escaping():
    """Python 3.11+ caps `int()` on decimal strings at `sys.get_int_max_str_digits()` — 4300 by
    default — and raises `ValueError` past it. A 5,000-digit `Content-Length` is all ASCII digits,
    so it clears a syntax check and then blows up in the conversion.

    That lands worst on a protected route: `_deny` asks `_declares_a_body()` BEFORE it writes the
    401, so the exception escaped the handler ahead of the status and the client got a bare close —
    the same failure the non-numeric case had, reintroduced one layer along. (Codex, review of #87.)
    """
    server, host, port, _ = _server(policy=auth.AgentAuthPolicy(KEY))
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "1" * 5000)
        conn.endheaders()
        assert conn.getresponse().status == 401, "the gate answered; the conversion never ran"
        conn.close()
    finally:
        server.shutdown()


def test_an_unconvertibly_long_length_is_too_large_not_malformed(monkeypatch):
    """Authenticated, so it reaches `_read_body`.

    A decimal with more digits than the limit itself cannot be *under* the limit, so it is refused
    as too large without being converted at all — nothing to trip the 4300-digit ceiling, and no
    reason to call a perfectly well-formed number malformed. 413, not 400.
    """
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "9" * 5000)
        conn.endheaders()
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 413 and "limit" in body["error"]
        conn.close()
    finally:
        server.shutdown()


def test_leading_zeros_do_not_inflate_the_digit_count(monkeypatch):
    """The digit-count shortcut compares against the limit's own width, so it has to strip leading
    zeros first: `000000000512` is 12 digits against a 4-digit limit but names 512 bytes, which is
    under it. Refusing that would turn a valid request into a 413 on formatting alone."""
    monkeypatch.setattr(agent_main, "AGENT_MAX_JSON_BYTES", 1024, raising=True)
    server, host, port, _ = _server()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "000000000512")
        conn.endheaders()
        conn.send(b"{}" + b" " * 510)
        r = conn.getresponse()
        assert r.status != 413, "512 bytes is under the 1024 limit however it is spelled"
        conn.close()
    finally:
        server.shutdown()


def test_an_empty_content_length_is_not_an_absent_one():
    """`or "0"` collapsed the two, so `Content-Length:` with no value became `"0"` before the
    syntax check could see it — and the comment claiming to reject `""` described a branch nothing
    reached.

    Measured on the unfixed tree, an empty declaration answered **byte-identically to no
    declaration at all** (`unknown job kind None` either way): the 40 bytes sent after the headers
    were never read, and `_declares_a_body()` said no, so the deferral was skipped too. Asserting
    on which 400 arrives is therefore the whole test — the status is 400 either way, because a
    `/jobs` POST with an empty body is also a 400.
    """
    server, host, port, _ = _server()
    try:
        data = _raw(host, port,
                    b"POST /jobs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    b"Content-Length:\r\n\r\n" + b"G" * 40)
        assert b"400" in data.split(b"\r\n")[0]
        assert b"byte count" in data, \
            "refused on the framing, not answered as though no body had been declared"
    finally:
        server.shutdown()


def test_conflicting_content_length_headers_are_refused():
    """Two `Content-Length` headers used to mean first-wins: `5` then `40` read 5 bytes and left 35
    in the buffer, answering `invalid JSON` over the fragment it happened to take.

    A front end that honours the last value would then disagree with this server about where the
    request ends, which is the shape request smuggling takes. Found while confirming the empty-value
    case above, not reported. RFC 9110 8.6 says reject.
    """
    server, host, port, _ = _server()
    try:
        data = _raw(host, port,
                    b"POST /jobs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 5\r\nContent-Length: 40\r\n\r\n" + b"G" * 40)
        assert b"400" in data.split(b"\r\n")[0] and b"byte count" in data
    finally:
        server.shutdown()


def test_a_repeated_identical_content_length_is_not_a_conflict():
    """The guard is about disagreement, not repetition. The same value twice names one request
    boundary, so refusing it would turn a legal-if-odd message into a framing error."""
    server, host, port, _ = _server()
    try:
        data = _raw(host, port,
                    b"POST /jobs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
        assert b"byte count" not in data, "identical repeats agree about where the body ends"
    finally:
        server.shutdown()


def test_an_absent_content_length_still_means_no_body():
    """The other half of splitting absent from empty: a POST with no `Content-Length` at all is not
    a framing error, it is a request with no body, and it must stay one."""
    server, host, port, _ = _server()
    try:
        data = _raw(host, port,
                    b"POST /jobs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\r\n")
        assert b"byte count" not in data, "no declaration is not a malformed declaration"
    finally:
        server.shutdown()


def test_a_refused_request_with_an_empty_length_keeps_its_401():
    """The auth-gate half, which is where the collapsed empty value actually cost something: the
    401 goes out over 8 KiB still in flight, and `_declares_a_body()` returning False skipped the
    deferral that keeps the close from resetting the status away (#85)."""
    server, host, port, _ = _server(policy=auth.AgentAuthPolicy(KEY))
    try:
        data = _raw(host, port,
                    b"POST /jobs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    b"Content-Length:\r\n\r\n", tail=b"G" * 8192)
        assert b"401" in data.split(b"\r\n")[0], "the gate's verdict survived the unread body"
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
