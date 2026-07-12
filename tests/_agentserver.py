"""Shared agent test server (020 T413; 023 US6 T535).

The 020 dual-runtime switch is retired: the on-hardware drill's verdict was keep-stdlib
(docs/on-hardware-validation.md), so 023 deleted `hostagent/asgi.py`, the `AGENT_RUNTIME` branch,
and the uvicorn test leg (FR-315). `RUNTIMES` survives as a one-element tuple so the HTTP suites'
transport parameterization stays in place — the shared behavior tests are retained verbatim; only
the duplicate-transport leg is gone.
"""
import threading
from http.server import ThreadingHTTPServer

from hostagent import main as agent_main

RUNTIMES = ("stdlib",)


class AgentServer:
    def __init__(self, base_url: str, shutdown):
        self.base_url = base_url
        self._shutdown = shutdown

    def shutdown(self):
        self._shutdown()


def start_agent(admission, journal, manager, jobs, runtime: str = "stdlib",
                policy=None) -> AgentServer:
    """`policy` (023 US2): an explicit AgentAuthPolicy for the auth suites. None = open (the
    pre-023 test default, so domain suites exercise routing without minting keys)."""
    if runtime != "stdlib":
        raise ValueError(f"unknown runtime {runtime!r} — the uvicorn transport was retired (023)")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_main.make_handler(admission, journal, manager, jobs,
                                                  policy=policy))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return AgentServer(f"http://127.0.0.1:{server.server_address[1]}", server.shutdown)
