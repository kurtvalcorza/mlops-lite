# Contract: Host-Agent Authentication and Request Bounds

## Trust boundary

The host agent is a privileged internal API. Network locality is not authorization. The gateway and
approved host tools authenticate to it with a generated internal credential distinct from the
operator-facing gateway API key.

## Configuration

- `AGENT_API_KEY`: preferred secret value, or an equivalent permission-restricted secret file.
- `AGENT_ALLOW_OPEN`: optional truthy development override; false/unset by default.
- `AGENT_CONTROL_SECRET`: one-release deprecated input accepted as the internal key only with a
  startup warning.
- `SWAP_CONTROL_SECRET`: legacy alias must not independently enable an unprotected agent.

Normal startup fails before socket bind when no usable key is configured. The message names the
generation/setup command but never the expected value.

## Request authentication

Protected requests carry:

```http
X-Agent-Key: <internal secret>
```

The agent compares with `hmac.compare_digest` before parsing a body or invoking domain code.

### Explicitly public

- `GET /healthz`: minimal process liveness only.
- `GET /readyz`: minimal readiness/compatibility status only.
- `GET /metrics`: Prometheus exposition; metric values/labels contain no secrets or unbounded IDs.

### Protected

- Operational `GET /health`, `/engines`, `/jobs`, legacy job status aliases.
- All inference routes under `/engines/*`.
- Job submission/status/cancel routes and legacy aliases.
- `/control/*`, reload, unload, and future lifecycle operations.

If infrastructure requires another public probe, it must be added to this exact allow-list with a
minimal response contract and tests. Prefix-based public matching is forbidden.

## Failure responses

- Missing key: `401` with `{"error":"agent authentication required"}`.
- Wrong key: `403` with `{"error":"agent authentication failed"}`.
- Authentication failure occurs before body parsing and produces no admission/job/store effects.
- `WWW-Authenticate` may identify the internal header scheme but must not reveal configured state.

## Gateway behavior

- All gateway HTTP clients targeting the agent inject `X-Agent-Key` from server-side settings.
- The BFF/browser never receives or accepts this key.
- Redirects to an origin different from the configured agent are disabled or strip the header.
- Error messages may name the agent endpoint but never echo headers.
- Key rotation closes/recreates pooled connections only as needed; authentication is per request.

## Open-development behavior

With `AGENT_ALLOW_OPEN=1` and no key, protected routes are open for throwaway local use. Startup and
health expose `security_mode="open-development"` without secret material, and logs print a prominent
warning. Generated `.env` files and documented standard bring-up never set this override.

## Request bounds

- Requests are admitted to a bounded worker pool and bounded pending queue.
- Authentication happens before a body is buffered.
- JSON defaults to 1 MiB; multipart defaults to 32 MiB unless hardware validation selects lower
  documented values.
- Declared `Content-Length` above the route limit receives 413 immediately.
- Streaming/chunked bodies are counted and aborted at the same limit.
- Saturation receives 503 or waits only within the bounded queue.
- Slow reads/writes and shutdown drain have explicit timeouts.

## Compatibility

Successful authorized responses preserve existing payload/status semantics. The only intended
contract change for existing direct callers is the new authentication requirement and explicit
413/503 responses at resource bounds.
