# Contract: slim children byte-compat (US2) + agent runtime switch (US3)

## Children (vision / embed / tabular) — the FR-203 boundary

The agent adapters are the ONLY consumers (FR-177). Each child keeps, byte-for-byte:

| Child | Route (POST) | Request | Response | Probe |
|---|---|---|---|---|
| vision | `/classify` | multipart image (field + content type unchanged) | JSON body identical to today's | `GET /readyz` → 200 |
| embed | `/embed` | JSON (texts) | JSON (vectors) — same shape/dtype formatting | `GET /readyz` → 200 |
| tabular | `/predict` | JSON (rows) | JSON (predictions) + `model_version` — see §Tabular served version | `GET /readyz` → 200 |

Launch contract (unchanged from R10/018): the adapter spawns `run.sh` with a dynamic port in a
process group; readiness = the probe; teardown = process-group signal. The ONLY permitted adapter
edits are the child launch path, the `unavailable` pip-hint string, and the cosmetic
`_bento_cpu.py` → `_child_cpu.py` module rename (with its import updates) — the adapter
*contract* (verbs, states, error mapping) is untouched. Error vocabulary at the
agent boundary (404 verb / 415 content-type / 5xx child failure) is unchanged.

### Tabular served version (025 US2, FR-353/FR-359 — amends the tabular row above)

The tabular child's `/predict` response gains ONE additive top-level field, `model_version`: the
registry version the rows were ACTUALLY scored by, or `null` when the registry was unreachable and
the env-fallback artifact is resident. Additive only — every pre-025 field keeps its name, type, and
value, so the GoldenSet byte-diff below is extended rather than broken, and consumers reading only
`predictions[i]` (the adapters, the batch flow) are unaffected.

The child also now RELOADS when the `@serving` alias moves, rather than re-resolving only on a cold
load: continuous traffic keeps the child warm, so idle-release never fired and a promote left the
old booster resident indefinitely. The reported version is what makes that observable, and the
gateway prefers it over its own registry read when attributing logged predictions — see
`specs/025-close-lifecycle-gaps/contracts/tabular-prediction-identity.md`.

Gate: the GoldenSet replay (data-model.md) diffs status + content type + body bytes at the agent
boundary per child, same machine/session. A drifted response fails the swap, not production.

Venv contract: after all three swaps, `bentoml` and its exclusive transitive deps are absent from
the host venv; `fastapi`+`uvicorn` are present ONCE (shared pin with US3); torch/torchvision,
sentence-transformers, lightgbm pins unchanged (SC-131).

## Agent runtime switch (US3) — the FR-205/206 boundary

- `AGENT_RUNTIME` env: `stdlib` (default until verdict) | `uvicorn`.
- Both runtimes serve the IDENTICAL route table by reusing the framework-free handlers
  (`forward_engine`, `forward_engine_multipart`, jobs/health/metrics/control) — no logic fork;
  admission/lifecycle/swap code is not re-entered by this change (Principle II analysis holds).
- Preserved regardless of runtime: `AGENT_BIND` posture, `X-Agent-Control` secret enforcement,
  the 008–017 error vocabulary, `/metrics` output shape, SSE stream framing for `stream_verbs`,
  legacy byte-compat route aliases.
- The drill (`scripts/agent_stream_drill.py`) writes a RuntimeBaselineRecord per run; the default
  flips only on a recorded `upgrade-uvicorn` verdict; the losing runtime's code path + switch are
  deleted in the next increment (no permanent dual matrix).
- Offline: the agent HTTP test suite runs against BOTH runtimes while the switch exists (same
  assertions, parameterized) — contract drift between transports is a test failure, not a drill
  surprise.
