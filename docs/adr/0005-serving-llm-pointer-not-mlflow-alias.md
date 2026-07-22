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
- The go-live sequence captures and, on failure, restores the prior pointer **together with** the
  alias move (FR-265 rollback). The host agent resolves the pointer at each cold load via
  `platformlib.llmresolve` (a duck-typed MLflow client, so the stdlib-only agent needs no mlflow).
- Unset pointer ⇒ adopt the sole promoted LLM, else the configured default (`active_serving_llm_name`).

## Consequences

- **Cross-model selection works**: base ↔ fine-tune go-live is expressible; a single alias could not
  do this. This is the whole reason the pointer exists.
- **Two facts to keep consistent** (the MLflow alias and the Postgres pointer), reconciled inside the
  one gated go-live sequence and recorded in the data-authority table. This is an accepted cost.
- **Rejected alternative — do NOT collapse the pointer into an MLflow alias.** It would lose
  cross-model selection and silently break base↔fine-tune switching. If someone proposes "just use an
  alias," this ADR is the answer.
- **Module-locality follow-up (feature 024):** the `serving_llm` pointer wrappers currently live in
  `gateway/app/registry.py` — the *MLflow adapter* — even though they are Postgres state. They should
  be relocated into a dedicated relational repository as part of the 024 store decomposition / go-live
  extraction (US1/US2), so the MLflow adapter stops carrying relational-store code.

## References

- Spec `specs/022-registry-driven-llm-serving/` (the pointer + gated go-live).
- `docs/current-architecture.md` — data-authority table (desired vs. resident).
- FR-265 (refuse/rollback), FR-276 (disambiguation when several LLMs hold a promoted alias).
- Feature `specs/024-deepen-modules-seams/` US1/US2 (the relocation follow-up).
