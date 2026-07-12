"""Host-agent internal authentication (023 US2, T499 — contracts/agent-security.md).

The agent is a privileged internal API: it loads models, runs jobs, and controls the GPU. Network
locality is NOT authorization (WSL port relay + `AGENT_BIND=0.0.0.0` make it reachable from the
LAN in common configurations), so every state-changing or state-revealing route requires the
generated internal credential — distinct from the operator-facing gateway key.

Fail-closed by construction (FR-284): with no usable key configured, `from_env()` raises before
the socket ever binds. The ONLY way to run open is the explicit `AGENT_ALLOW_OPEN` development
override, which warns prominently and is never the generated/documented default (FR-285).

Stdlib-only (the agent carries no pip deps): `hmac.compare_digest` for the comparison, plain env/
file reads for the key.
"""
import hmac
import os

#: The EXACT public allow-list (FR-283). Prefix matching is forbidden (contracts/agent-security.md):
#: the richer `/health`, `/engines`, `/jobs` views expose holder/lease/engine/job state and stay
#: behind the key. Adding a probe here requires a minimal response contract + tests.
PUBLIC_ROUTES = frozenset({("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/metrics")})

#: Stable failure payloads (contracts/agent-security.md §Failure responses). Missing vs wrong are
#: distinct so an operator can tell a mis-wired client from a rotated key; neither reveals
#: configured state.
MISSING = (401, {"error": "agent authentication required"})
WRONG = (403, {"error": "agent authentication failed"})

_TRUTHY = ("1", "true", "yes", "on")


class AgentAuthConfigError(Exception):
    """No usable key and no explicit open-development override — refuse to start (FR-284)."""


class AgentAuthPolicy:
    """The resolved credential policy — a plain object so tests construct it directly and the
    transports never re-read the environment per request."""

    def __init__(self, key: str = "", *, allow_open: bool = False,
                 deprecated_source_used: bool = False):
        self.key = key or ""
        self.allow_open = bool(allow_open)
        self.deprecated_source_used = bool(deprecated_source_used)

    @property
    def security_mode(self) -> str:
        return "key" if self.key else "open-development"

    def authorize(self, method: str, path: str, provided: str):
        """None when the request may proceed, else the (status, payload) to answer with.

        Called BEFORE any body read/parse or domain call (FR-282): a refused request produces no
        admission, journal, store, or child-lifecycle effect. Comparison is constant-time
        (`hmac.compare_digest`) so the key can't be probed byte-by-byte."""
        if (method.upper(), path) in PUBLIC_ROUTES:
            return None
        if not self.key:  # open-development (from_env refuses this without the explicit override)
            return None
        if not provided:
            return MISSING
        if not hmac.compare_digest(provided.encode(), self.key.encode()):
            return WRONG
        return None


def _read_key_file(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def from_env(env=None, log=print) -> AgentAuthPolicy:
    """Resolve the policy from the environment (contracts/agent-security.md §Configuration).

    Precedence: `AGENT_API_KEY` > `AGENT_API_KEY_FILE` (permission-restricted secret file) >
    deprecated `AGENT_CONTROL_SECRET` (accepted for ONE release, with a startup warning naming the
    replacement). `SWAP_CONTROL_SECRET` never independently enables anything — it predates the
    agent and its continued presence must not silently stand in for the internal key.

    Raises AgentAuthConfigError when nothing usable is configured and `AGENT_ALLOW_OPEN` is not
    set — the message names the generation command but NEVER any expected/partial value."""
    env = os.environ if env is None else env
    key = (env.get("AGENT_API_KEY") or "").strip()
    deprecated = False
    if not key:
        key_file = (env.get("AGENT_API_KEY_FILE") or "").strip()
        if key_file:
            try:
                key = _read_key_file(key_file)
            except OSError as e:
                raise AgentAuthConfigError(
                    f"AGENT_API_KEY_FILE is set but unreadable ({e.__class__.__name__}) — fix the "
                    f"file or set AGENT_API_KEY (scripts/gen_secrets)") from e
    if not key:
        legacy = (env.get("AGENT_CONTROL_SECRET") or "").strip()
        if legacy:
            key = legacy
            deprecated = True
            log("!! AGENT_CONTROL_SECRET is deprecated as the agent credential — set AGENT_API_KEY "
                "(scripts/gen_secrets). This fallback is removed next release.", flush=True)
    allow_open = (env.get("AGENT_ALLOW_OPEN") or "").strip().lower() in _TRUTHY
    if not key:
        if not allow_open:
            raise AgentAuthConfigError(
                "no agent credential configured — set AGENT_API_KEY (run scripts/gen_secrets and "
                "restart), or set AGENT_ALLOW_OPEN=1 for throwaway local development only (FR-284)")
        log("!! AGENT_ALLOW_OPEN: the host agent is running UNAUTHENTICATED (open-development "
            "mode). Anyone who can reach this port can load models and run jobs. Never use this "
            "beyond throwaway local development (FR-285).", flush=True)
    return AgentAuthPolicy(key, allow_open=allow_open, deprecated_source_used=deprecated)
