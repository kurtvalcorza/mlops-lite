# ADR 0005 — The platform serving-LLM selection is a Postgres pointer, not an MLflow alias

**Status**: Accepted (documents a decision made in spec 022; recorded here under feature 024 US4)

## Context

MLflow is the platform's registry authority: it owns model versions, gate metrics, and the
per-model `@serving` alias (see `docs/current-architecture.md` data-authority table). A natural
question — asked more than once — is why "which model serves text-generation right now" is **not**
just another MLflow `@serving` alias.

The blocking fact: **an MLflow `@serving` alias is scoped to a single registered model.** But the
platform must express **one global, cross-model** selection — and the candidates are *different
registered models*:

- a base model (e.g. `qwen2.5-...`) and a fine-tune of it are **distinct registered-model names**;
- promoting the fine-tune means text-generation should now serve a *different registered model*, not
  a different version of the same one.

An alias on model A cannot point at model B, so no per-model alias can name "the platform's serving
LLM" when that selection ranges over several models. Separately, the alias/pointer is the *desired*
selection; the **actually-resident** model is the host agent's own authority (health-reported) — a
third, deliberately distinct fact.

## Decision

- Persist the desired serving-LLM as a **`serving_llm` pointer row in the gateway Postgres DB**
  (`platformlib.store`), written only by the gated `registry.promote` go-live path.
- Keep MLflow as the authority for per-model `@serving` aliases, versions, gate metrics, tracking,
  and traces — unchanged.
- On an unresolvable reload the go-live sequence restores the prior **pointer** only
  (`registry.restore_serving_llm`, FR-265 rollback — `gateway/app/activation.py:_roll_back`); the candidate
  model's MLflow `@serving` alias **remains moved**. Rollback restores the global pointer but does NOT move
  the per-model alias back (consistent with `specs/023-platform-architecture-hardening/spec.md:290`), so
  recovery work must treat the alias as already advanced while the pointer names the prior serving LLM. The
  host agent resolves the pointer at each cold load via `platformlib.llmresolve` (a duck-typed MLflow client,
  so the stdlib-only agent needs no mlflow).
- Unset pointer ⇒ adopt the sole promoted LLM, else the configured default (`active_serving_llm_name`).

## Consequences

- **Cross-model selection works**: base ↔ fine-tune go-live is expressible; a single alias could not
  do this. This is the whole reason the pointer exists.
- **Two facts to keep consistent** (the MLflow alias and the Postgres pointer), reconciled inside the
  one gated go-live sequence and recorded in the data-authority table. This is an accepted cost.
- **Rejected alternative — do NOT collapse the pointer into an MLflow alias.** It would lose
  cross-model selection and silently break base↔fine-tune switching. If someone proposes "just use an
  alias," this ADR is the answer.
- **Module-locality follow-up (feature 024):** the `serving_llm` pointer **CRUD** primitives
  (`get`/`set`/`restore`) currently live in `gateway/app/registry.py` — the *MLflow adapter* — even
  though they are pure Postgres state. They should be relocated into a dedicated relational repository
  as part of 024 (US1/US2). **`active_serving_llm_name` does NOT move into that repository**: it reads
  the pointer AND resolves cross-authority (pointer-unset → `llmresolve.adopt_active_llm`, an MLflow
  call, → configured default), so it stays a higher-level web-free selection policy — the relational
  layer must not import MLflow.

## References

- Spec `specs/022-registry-driven-llm-serving/` (the pointer + gated go-live).
- `docs/current-architecture.md` — data-authority table (desired vs. resident).
- FR-265 (refuse/rollback), FR-276 (disambiguation when several LLMs hold a promoted alias).
- Feature `specs/024-deepen-modules-seams/` US1/US2 (the relocation follow-up).
