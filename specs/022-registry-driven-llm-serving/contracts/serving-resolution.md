# Contract: registry-driven LLM serving resolution

Defines how the active serving-LLM is selected, resolved to an artifact set, and reloaded — the
backend contract 022 must satisfy. Behaviour-level; concrete function/endpoint names are the plan's
Structure Decision.

## The active serving-LLM pointer (R1)

- There is exactly **one** platform-scoped active text-generation model selection at a time.
- Reading it returns the selected `model_name`, or the configured default base when unset.
- Setting it is an operator action routed through the **existing gated promotion choke-point** (011/
  015) for the chosen model+version; on success it (a) moves that model's `@serving` alias to the
  chosen version and (b) records the model as the active serving LLM.
- Setting it MUST trigger a controlled agent reload so the change is live (see Reload).

## Resolution (host agent, each cold load — R2/R3)

Given the active `model_name`:

1. Resolve its `@serving` version from the registry.
2. Read that version's `kind`:
   - `full-model` → `{ base_gguf = version.source, adapter_gguf = null }`.
   - `lora-adapter` → `{ adapter_gguf = version.source, base_gguf = resolve(base_model) }`, where
     `resolve(base_model)` maps the version's `base_model` lineage to a **registered base version's**
     `source` GGUF (a local/zoo file).
3. The engine spawns `llama-server -m <base_gguf> [--lora <adapter_gguf>]`.

**Base-resolution rule (no multi-hop chains)**: an adapter's `base_model` MUST resolve **directly** to
a `kind=full-model` text-generation version (a base GGUF). An adapter whose `base_model` points at
**another adapter** is a **resolution error** (refused per FR-265), not a multi-hop walk — LLM LoRA
fine-tunes train from a full base, so a single hop is sufficient and avoids ambiguous stacked-adapter
serving (spec review PR #64, R2 note).

**Invariants**:
- Resolution is a pure read of {active pointer, registry}. No network fetch; the base + adapter GGUFs
  MUST already be present locally.
- If the `@serving` version, the artifact, or the base cannot be resolved/found, resolution FAILS
  with a clear reason; the select/promote is refused and the currently-served LLM is **unchanged**
  (FR-265). Never a wedged/empty serving state.
- Resolution is picked up on the next controlled (re)load — no process restart, no host config edit
  (FR-255). This mirrors the vision/embed/tabular children resolving `@serving` each cold load.

## Reload under the single-GPU lease (R4 — Principle II)

Admission tenancy is **per-engine** (`"llm"`), not per-model, so a served-LLM switch has two cases —
both strictly sequential, **never two models resident** (FR-257/SC-147):

- **Cross-tenant** (a non-LLM engine or a job holds the GPU): reuse the existing swap
  (`hostagent/swap.py:preempt_for`, `evict → free → load`). A resident **serving** model is displaced
  only after operator confirm (naming the holder, FR-258); a **training/HPO/batch job** is
  **refused/deferred** (never preempted, FR-259/SC-150).
- **Same-tenant model switch** (the `llm` engine is already resident with a *different* model — the
  common US1 case): `preempt_for` is a **no-op** (it treats the same engine tenant as satisfied and
  `ensure_loaded()` short-circuits when resident+ready). This case MUST perform an explicit
  **force-reload — unload the `llm` child → `ensure_loaded()`** — so `llama-server` re-spawns with the
  newly resolved base+adapter (FR-255 immediate reload / SC-144). Reusing `preempt_for` alone here
  silently keeps serving the old GGUF (spec review PR #64, §1).
- If the resolved target is the **same** model+version already resident, the reload is an idempotent
  no-op (no force-reload).
- The target is probed (available / not-wedged) **before** any eviction, so a bad artifact never
  evicts a working holder (existing swap guard).

## Byte-compatibility (FR-271)

- `POST /infer` and `POST /infer/stream` request/response shapes are unchanged. A full-model serve is
  byte-for-byte today's behavior (SC-149); an adapter serve differs only in the produced text.
