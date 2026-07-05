# Data Model: Registry-Driven LLM Serving & Operator Model Selection

022 introduces **one new persisted pointer** (the active serving-LLM selection) and **generalizes
existing registry records** (LLM version descriptors + base references). Everything else is a read
model over the MLflow registry + the relational store. No new store or bucket.

## Persisted

### ActiveServingLLM (NEW — platform-scoped, one row)
The single source of truth for which text-generation **model** is live.
- `model_name`: the registered text-generation model selected to serve (spans model names — R1).
- `selected_at`, `selected_by` (operator/audit).
- `default`: absent/unset ⇒ the configured base model serves (today's behavior).
- Written by the console's promote/"set serving LLM" action; read by the host-agent resolver on each
  cold load. Lives in the relational store (018 US4), not an MLflow alias (R1).

### ServedPredictionIdentity (GENERALIZED)
Each logged prediction records the model **name + version that actually produced it** (agent-reported,
R5), not a fixed config string — so the quality window (013) keys correctly. No new table; the
existing prediction record's `model_name`/`model_version` fields are populated from the agent identity.

## Registry records (MLflow — generalized, no new entity)

### LLMVersion (text-generation registered version)
- `name`, `version`, `source` (artifact URI).
- `tags.task` = `text-generation` (**now always stamped** — FR-266; backfilled for legacy — FR-267).
- `tags.serving_engine` = `llama.cpp`.
- `tags.kind` ∈ { `full-model`, `lora-adapter` } — the base-vs-adapter discriminator (FR-268).
- `tags.base_model` (adapters only): reference to the base — resolvable to a **registered base
  version** (R2). Plus existing lineage (`parent`, `dataset_name`, `dataset_version`, eval tags).
- **Full-model** (`kind=full-model`): `source` is a standalone GGUF served with `-m source`.
- **Adapter** (`kind=lora-adapter`): `source` is the adapter GGUF; served as `-m <base> --lora <source>`
  where `<base>` is resolved from `base_model` → a registered full-model version's `source`.

### BaseModelVersion (a full-model LLM version used as an adapter's base)
A registered `kind=full-model` text-generation version whose `source` resolves to a local/zoo GGUF.
Registered via `scripts/register_base_gguf.py` so a fine-tune's `base_model` references a first-class
registry record, not a bare filesystem path (R2/R7).

## Read models (resolution — from the registry + the active pointer)

### ServingLLMResolution — resolved at each cold load (host agent, R3)
`ActiveServingLLM.model_name` → that model's `@serving` version → its `LLMVersion` →
`{ model_name, version, base_gguf, adapter_gguf|null }`.
- Full-model: `base_gguf = source`, `adapter_gguf = null`.
- Adapter: `adapter_gguf = source`, `base_gguf = resolve(base_model → registered base version.source)`.
  `base_model` MUST resolve **directly** to a `kind=full-model` version; an adapter pointing at another
  adapter is a resolution error (no multi-hop chain — PR #64 R2 note).
- If the base is unresolvable/absent ⇒ **resolution error** → the promote/select is refused, the
  currently-served LLM unchanged (FR-265).

### ServedIdentity — `GET /serving/state` + logged predictions (R5)
- `holder`, `resident` (lease state, unchanged).
- `serving_model` = the **agent-reported** loaded model name (not the fixed config `SERVING_MODEL`).
- `serving_version` = the **agent-reported** loaded registry version.
- `base`, `adapter` (optional): the resolved base + adapter identity for a served fine-tune (FR-274).

### LLMServingTarget — `GET /serving/tasks` (generalized)
The text-generation entry now always carries a non-null `task` (FR-266/267) and its base-vs-adapter
`kind` + lineage, so the console renders a real LLM panel and shows what would be promoted (FR-268/270).
The text-generation target is **filtered/preferred to the active-serving-LLM pointer** (FR-276): since
a promote moves only its own model's `@serving` alias, several LLM models can each retain a promoted
alias — but only the active-pointer model is the live target, so the list surfaces exactly one live
LLM, no stale duplicate (PR #64 §3).

## State transitions (all lease-safe; the UI only reflects them)

- **Select/promote serving LLM**: `ActiveServingLLM.model_name` moves → an agent reload is requested
  (evict → load) under admission (R4). Resident serving holder ⇒ operator-confirmed; running job ⇒
  refused/deferred (never preempted). Target already resident ⇒ idempotent no-op.
- **Load failure** (bad/missing artifact): the reload aborts before eviction (target-probe) or
  restores the prior model; serving is never left empty (edge case).
- **Served identity**: follows the actual resident model — updated when a reload completes; reported
  by the agent, consumed by the gateway (never independently re-derived).
