# Contract: honest served identity + BFF allow-list delta

## Agent-reported served identity (R5)

The host agent is the **only** component that knows what is actually resident. It MUST report, in its
LLM engine health (and surfaced through the aggregate health the gateway reads):

- `model_name` — the registry model name of the loaded LLM.
- `registry_version` — the registry version actually loaded.
- `base` / `adapter` (optional) — the resolved base + adapter identity when serving a fine-tune.

The gateway MUST **consume** these rather than re-deriving from a fixed config value:

- `GET /serving/state` (`serving.gpu_state`) reports the agent's `model_name` + `registry_version`
  (not the fixed `SERVING_MODEL`). When the agent is unreachable, it degrades to `unknown`, never to
  a stale config guess (consistent with the 021 `useLiveState` degradation).
- **Prediction logging** (`routers/infer.py`, `routers/stream.py`) attributes each served prediction
  to the agent-reported `model_name` + `registry_version` — so the quality window (013) scores the
  correct model+version (FR-261). No prediction is logged under a model the agent is not serving.

**Invariant (FR-262)**: the inference response's model identity, the `/serving/state` identity, and
the logged prediction identity all name the same model+version — the one actually resident.

## BFF allow-list delta (FR-272)

Any new gateway route the console calls to (a) read the active serving-LLM / resident-vs-promoted
delta or (b) trigger the set-serving/reload MUST be added to `ui/lib/gw-allowlist.ts` explicitly, and
the delta enumerated here, so the reachable surface stays auditable (021 discipline). Candidates
(final names per the plan/tasks):

| Method | Pattern | For |
|---|---|---|
| GET | `serving/llm` (or reuse `serving/state`) | active serving-LLM + resident-vs-promoted delta (FR-269) |

- **Promote = go live** (Clarifications 2026-07-05): there is **no separate "select"/"set-serving"
  action** — promoting a text-generation version through the **already-allow-listed**
  `POST models/:name/promote` *is* the switch (it moves the alias, writes the active-serving-LLM
  pointer, and triggers the immediate controlled reload, FR-255). So the switch adds **no new route**;
  the only possible addition is a read for the resident-vs-promoted delta (and even that prefers
  reusing `GET serving/state`).
- **Prefer reusing existing allow-listed routes** so the delta is minimal or empty.
- No wildcard, no broadening beyond the enumerated additions; a view issuing a non-listed call fails
  closed at the BFF (021 FR-251 lineage).
