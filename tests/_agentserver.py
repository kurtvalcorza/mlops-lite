"""Shared dual-runtime agent test server (020 T413).

While the `AGENT_RUNTIME` switch exists, the agent HTTP suites run their assertions against BOTH
transports (contracts/children-api.md §runtime: contract drift between transports is a test
failure, not a drill surprise). `start_agent(...)` serves the same wired components over either
the stdlib `ThreadingHTTPServer` or an in-thread uvicorn `Server`; tests parameterize on
`RUNTIMES`. The uvicorn leg skips cleanly where uvicorn isn't installed (the agent itself is
pip-dep-free by default — uvicorn rides the serving/children pin in the host venv).
"""
import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from hostagent import main as agent_main

RUNTIMES = ("stdlib", "uvicorn")


class AgentServer:
    def __init__(self, base_url: str, shutdown):
        self.base_url = base_url
        self._shutdown = shutdown

    def shutdown(self):
        self._shutdown()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_agent(admission, journal, manager, jobs, runtime: str, policy=None) -> AgentServer:
    """`policy` (023 US2): an explicit AgentAuthPolicy for the auth suites. None = open (the
    pre-023 test default, so domain suites exercise routing without minting keys)."""
    if runtime == "stdlib":
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), agent_main.make_handler(admission, journal, manager, jobs,
                                                      policy=policy))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return AgentServer(f"http://127.0.0.1:{server.server_address[1]}", server.shutdown)

    if runtime != "uvicorn":
        raise ValueError(f"unknown runtime {runtime!r}")
    uvicorn = pytest.importorskip("uvicorn")
    from hostagent.asgi import build_asgi_app

    port = _free_port()
    config = uvicorn.Config(build_asgi_app(admission, journal, manager, jobs),
                            host="127.0.0.1", port=port, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        if not thread.is_alive():
            raise RuntimeError("uvicorn server thread died during startup")
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("uvicorn did not start within 15s")

    def shutdown():
        server.should_exit = True
        thread.join(timeout=10)

    return AgentServer(f"http://127.0.0.1:{port}", shutdown)
