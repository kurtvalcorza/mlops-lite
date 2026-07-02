# Contract: `platformlib/` shared package

Stdlib-only (R2). Imported by the gateway image (Dockerfile `COPY platformlib/ …`) and the
host venv (repo path). Never imports gateway, hostagent, torch, or pydantic.

## `platformlib.topology`

- `Tenant` — canonical tenant ids: `LLM="llm"`, `ASR="asr"`, `VISION="vision"`,
  `TRAINING="training"` (replaces the scattered string literals; `llm-serving` legacy string
  mapped during migration only).
- `ENGINES` — the adapter registry rows (engine_id, gpu, optional, default enable) —
  data-model.md §EngineAdapter.
- `AGENT_PORT = 8100`, `AGENT_URL` resolution (env override → default), legacy `*_URL`
  resolution helpers used by the gateway settings module during migration phases.
- `NON_PREEMPTABLE_KINDS = {"job"}` — the single definition swap logic consults.
- `STATE_DIR` — the fixed non-`/tmp` host state dir (FR-166): lease interop file, journal,
  agent logs.

## `platformlib.contracts`

Dataclasses + `validate()` (raise `ContractError`) + `to_json()/from_json()`:

`AgentHealth`, `EngineState`, `AdmissionRequest/Result`, `JobSubmit`, `JobRecord`,
`ModelPolicy`, `PendingRetrain`, `PromotionSuggestion`, `AuditRecord`, `SwapCommand`,
`UnloadResult`. Unknown JSON fields are ignored on parse (forward compatibility); missing
required fields raise. These are the **only** shapes the gateway and agent exchange — a field
change is a change to this package, reviewed once.

## `platformlib.store`

- Object side (now): the S3 helpers promoted out of `gateway/app/quality.py`/`datasets.py`
  privates — module-level client reuse, paginated `list_keys`, `get/put_json`, `missing` —
  ending the fresh-client-per-call pattern and cross-module private reach-through.
- Relational side (US4): `connect()` (env-driven DSN, loopback), idempotent `bootstrap()` DDL
  (R4), typed accessors: `insert_prediction`, `attach_label` (write-once via unique
  constraint → `LabelExists`), `window(modality, model, version, n)`, `job_*` CRUD,
  `policy_*` CRUD, `suggestion_*`. Both runtimes call these directly (clarify Q4).

## Compatibility rules

- Additive evolution only within 018; removals require a major note in the package docstring.
- The gateway MUST NOT import `hostagent`; the agent MUST NOT import `gateway.app` — anything
  shared moves here. (Retires the `sys.path` dual-runtime hacks: `quality.py`, `batch.py`,
  `shadow.py`, `scoring/`, `hpo.py` import seams migrate to `platformlib` in their touching
  phases.)
