"""023 US2 (T497, FR-281..288) — the host agent's internal-credential trust boundary.

Offline, GPU-free: policy unit tests + a real stdlib server on an ephemeral port over fake
components. Pins the contract (contracts/agent-security.md):

  - key resolution: `AGENT_API_KEY` > `AGENT_API_KEY_FILE` > deprecated `AGENT_CONTROL_SECRET`
    (warns, one release); `SWAP_CONTROL_SECRET` never independently enables anything;
  - fail-closed startup: no key + no `AGENT_ALLOW_OPEN` refuses to start, and the refusal names
    the fix but never any secret material;
  - the EXACT public allow-list (`GET /healthz|/readyz|/metrics`) — `/health`, `/engines`,
    `/jobs` and every POST are protected; no prefix matching;
  - stable failure payloads: missing -> 401 {"error":"agent authentication required"},
    wrong -> 403 {"error":"agent authentication failed"};
  - authentication precedes side effects: a refused POST /jobs submits nothing;
  - the comparison runs through `hmac.compare_digest` (constant-time seam);
  - `/healthz` is a minimal liveness shape (no holder/engine/job state), `/metrics` renders
    without the key appearing anywhere in the exposition.
"""
import json
import os
import sys
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _agentserver import start_agent  # noqa: E402
from _agentstore import FakeJobStore  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import (  # noqa: E402
    auth,
    lifecycle,
)
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent.journal import Journal  # noqa: E402

KEY = "k-0123456789abcdef0123456789abcdef"


# --- policy resolution (from_env) ------------------------------------------------------------------

def test_env_key_wins(capsys):
    p = auth.from_env({"AGENT_API_KEY": KEY, "AGENT_CONTROL_SECRET": "legacy"})
    assert p.key == KEY and p.security_mode == "key" and not p.deprecated_source_used


def test_key_file_source(tmp_path):
    f = tmp_path / "agent.key"
    f.write_text(KEY + "\n")
    p = auth.from_env({"AGENT_API_KEY_FILE": str(f)})
    assert p.key == KEY and p.security_mode == "key"


def test_unreadable_key_file_fails_closed(tmp_path):
    with pytest.raises(auth.AgentAuthConfigError) as e:
        auth.from_env({"AGENT_API_KEY_FILE": str(tmp_path / "absent.key")})
    assert "gen_secrets" in str(e.value)


def test_deprecated_control_secret_accepted_with_warning():
    warnings = []
    p = auth.from_env({"AGENT_CONTROL_SECRET": KEY},
                      log=lambda msg, **kw: warnings.append(msg))
    assert p.key == KEY and p.deprecated_source_used
    assert any("deprecated" in w and "AGENT_API_KEY" in w for w in warnings)
    assert not any(KEY in w for w in warnings)  # the warning never echoes the secret (FR-288)


def test_swap_control_secret_never_enables_the_agent():
    """The pre-018 legacy alias must not independently stand in for the internal key."""
    with pytest.raises(auth.AgentAuthConfigError):
        auth.from_env({"SWAP_CONTROL_SECRET": "legacy-swap"})


def test_no_key_refuses_startup_and_redacts():
    with pytest.raises(auth.AgentAuthConfigError) as e:
        auth.from_env({})
    msg = str(e.value)
    assert "AGENT_API_KEY" in msg and "gen_secrets" in msg  # names the fix (FR-284)


def test_allow_open_is_explicit_and_warns():
    warnings = []
    p = auth.from_env({"AGENT_ALLOW_OPEN": "1"}, log=lambda msg, **kw: warnings.append(msg))
    assert p.security_mode == "open-development"
    assert any("UNAUTHENTICATED" in w for w in warnings)  # prominent (FR-285)


# --- authorize() ------------------------------------------------------------------------------------

def _policy():
    return auth.AgentAuthPolicy(KEY)


@pytest.mark.parametrize("method,path", sorted(auth.PUBLIC_ROUTES))
def test_public_routes_pass_without_key(method, path):
    assert _policy().authorize(method, path, "") is None


@pytest.mark.parametrize("path", ["/health", "/engines", "/jobs", "/engines/llm/health",
                                  "/healthz/", "/metricsx"])
def test_protected_and_near_miss_paths_require_key(path):
    """No prefix/near-miss matching (contracts/agent-security.md): the allow-list is exact."""
    assert _policy().authorize("GET", path, "") == auth.MISSING


def test_post_to_a_public_path_is_still_protected():
    assert _policy().authorize("POST", "/healthz", "") == auth.MISSING


def test_missing_vs_wrong_key_payloads():
    assert _policy().authorize("POST", "/jobs", "") == \
        (401, {"error": "agent authentication required"})
    assert _policy().authorize("POST", "/jobs", "nope") == \
        (403, {"error": "agent authentication failed"})
    assert _policy().authorize("POST", "/jobs", KEY) is None


def test_comparison_is_constant_time(monkeypatch):
    """The comparison must run through hmac.compare_digest — the constant-time seam (FR-287)."""
    calls = []
    import hmac as hmac_mod
    real = hmac_mod.compare_digest

    def spy(a, b):
        calls.append(True)
        return real(a, b)

    monkeypatch.setattr("hostagent.auth.hmac.compare_digest", spy)
    _policy().authorize("POST", "/jobs", KEY)
    assert calls, "authorize() did not use hmac.compare_digest"


# --- HTTP end-to-end (stdlib transport; FR-281/282/283) ---------------------------------------------

def _serve(policy):
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(store=FakeJobStore())
    manager = lifecycle.EngineManager(admission, runtimes={})
    jobs = jobs_mod.JobManager(admission, journal)
    server = start_agent(admission, journal, manager, jobs, "stdlib", policy=policy)
    return server, journal


def _req(base, path, method="GET", key=None, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if key is not None:
        req.add_header("X-Agent-Key", key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def test_http_gate_and_public_probes():
    server, journal = _serve(auth.AgentAuthPolicy(KEY))
    try:
        base = server.base_url
        # public: minimal liveness — none of the rich operational fields (FR-283)
        code, body, _ = _req(base, "/healthz")
        assert code == 200 and body == {"ok": True, "service": "hostagent"}
        code, body, _ = _req(base, "/readyz")
        assert code == 200 and body["ready"] is True
        # /metrics is public and never contains the key
        req = urllib.request.Request(base + "/metrics")
        with urllib.request.urlopen(req, timeout=5) as r:
            exposition = r.read().decode()
        assert r.status == 200 and KEY not in exposition
        # protected reads: missing -> 401, wrong -> 403, right -> 200 with security_mode
        code, body, _ = _req(base, "/health")
        assert (code, body) == (401, {"error": "agent authentication required"})
        code, body, _ = _req(base, "/health", key="wrong")
        assert (code, body) == (403, {"error": "agent authentication failed"})
        code, body, _ = _req(base, "/health", key=KEY)
        assert code == 200 and body["security_mode"] == "key"
        assert "holder" in body  # the rich view stays intact for the authenticated gateway
        code, _, _ = _req(base, "/engines", key=KEY)
        assert code == 200
    finally:
        server.shutdown()


def test_refused_post_produces_no_side_effect():
    """FR-282: authentication precedes admission/journal/store effects — a refused job submit
    stores nothing."""
    server, journal = _serve(auth.AgentAuthPolicy(KEY))
    try:
        base = server.base_url
        body = {"kind": "not-a-kind", "request": {}}
        code, _, _ = _req(base, "/jobs", method="POST", body=body)
        assert code == 401 and journal.jobs() == []
        code, _, _ = _req(base, "/jobs", method="POST", key="wrong", body=body)
        assert code == 403 and journal.jobs() == []
        # WITH the key, the SAME invalid body reaches domain validation (400 "unknown kind") —
        # proof the auth gate sits strictly BEFORE route/body handling, and was the only blocker.
        code, payload, _ = _req(base, "/jobs", method="POST", key=KEY, body=body)
        assert code == 400 and "unknown job kind" in payload["error"]
        assert journal.jobs() == []  # invalid submits never journal either
    finally:
        server.shutdown()


def test_open_development_mode_serves_without_key():
    server, _ = _serve(auth.AgentAuthPolicy())  # explicit open policy (AGENT_ALLOW_OPEN path)
    try:
        code, body, _ = _req(server.base_url, "/health")
        assert code == 200 and body["security_mode"] == "open-development"
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
