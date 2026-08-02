"""027 T717 — the BFF proxy-surface delta (contracts/allowlist-delta.md).

The allowlist is the console's security boundary: the BFF injects the operator key for these routes
**only**, so any gateway path not listed cannot ride that key. Adding a proxy route is therefore a
deliberate, reviewable act, which is why it gets a test rather than a convention.

The load-bearing assertion here is the negative one: **no agent path may appear.** The console never
reaches `:8100`. The gateway is the only holder of `X-Agent-Key` (023 US2, research R5), and the
`runtime/*` entries are the *gateway's* proxy routes — a same-named agent path in this list would
quietly relocate the trust boundary.

Parsed as text rather than executed: the file is TypeScript, and a Node round-trip to read a constant
list would make an offline Python suite depend on a JS toolchain for no additional confidence.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

ALLOWLIST_PATH = os.path.join(REPO, "ui", "lib", "gw-allowlist.ts")

_ENTRY = re.compile(r"\{\s*method:\s*'([A-Z]+)'\s*,\s*pattern:\s*'([^']+)'\s*\}")


def _entries():
    with open(ALLOWLIST_PATH, encoding="utf-8") as fh:
        return _ENTRY.findall(fh.read())


def test_the_console_read_routes_are_allowlisted():
    patterns = {pattern for method, pattern in _entries() if method == "GET"}
    for required in ("console/health", "console/capabilities", "runtime/hosts",
                     "runtime/hosts/:host/devices", "runtime/engines", "runtime/admission",
                     "runtime/journal"):
        assert required in patterns, f"{required} is not proxyable, so its panel cannot load"


def test_no_agent_path_appears_in_the_allowlist():
    """The console never reaches :8100. A same-named agent path here would relocate the trust
    boundary without anyone editing a security module."""
    # Checked against the ENTRY PATTERNS, not the file text: the comments legitimately mention the
    # agent port while explaining why it is absent, and a test that greps the prose would fail on
    # its own documentation.
    patterns = {pattern for _method, pattern in _entries()}
    for agent_only in ("engines", "control/unload", "control/reload", "runtime/devices",
                       "journal", "gpu/queue"):
        assert agent_only not in patterns, f"{agent_only!r} is an AGENT path, not a gateway one"

    for pattern in patterns:
        assert not pattern.startswith("agent/"), pattern
        assert ":8100" not in pattern, pattern


def test_the_console_surface_is_read_only():
    """A write here would be a control the console is not supposed to have — FR-379 forbids any
    job-preempting control, and the runtime area ships with no buttons at all."""
    for method, pattern in _entries():
        if pattern.startswith(("runtime/", "console/")):
            assert method == "GET", f"{method} {pattern} is not a read"


def test_the_broker_admin_surface_is_allowlisted_but_not_the_tenant_surface():
    """Tenants hold their own keys and call the gateway directly over TLS. Proxying `/v1/*` through
    the console's operator credential would let any browser session spend any tenant's quota."""
    patterns = {pattern for _m, pattern in _entries()}
    assert "admin/queue" in patterns and "admin/usage" in patterns
    for tenant_route in ("v1/chat/completions", "v1/embeddings", "v1/usage",
                         "v1/audio/transcriptions"):
        assert tenant_route not in patterns, f"{tenant_route} must not be proxyable by the console"


def test_every_entry_is_wellformed():
    for method, pattern in _entries():
        assert method in ("GET", "POST", "PUT", "DELETE"), method
        assert not pattern.startswith("/"), f"{pattern} — patterns are relative to /api/gw/"
        assert " " not in pattern


def test_the_matcher_requires_an_exact_segment_count():
    """A pattern matching a prefix would let one allowlisted route open a family of unlisted ones."""
    source = open(ALLOWLIST_PATH, encoding="utf-8").read()
    assert "pat.length !== segments.length" in source, \
        "the matcher must compare segment counts, not prefixes"
