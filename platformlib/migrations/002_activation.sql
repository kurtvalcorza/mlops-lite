-- 002_activation: durable LLM activation operations (023 US5 T520, FR-305/306/314 —
-- contracts/promotion-activation.md, data-model.md §ActivationOperation).
--
-- IMMUTABLE after merge — evolve with a new numbered file.
--
-- One operator go-live action spans MLflow (alias), Postgres (ActiveServingLLM pointer), and the
-- host agent (resident model); they cannot share a transaction, so this record makes the action
-- recoverable: every transition is a compare-and-set on `state`, and the two partial unique
-- indexes enforce (a) at most ONE non-terminal activation platform-wide and (b) idempotency-key
-- uniqueness among non-terminal operations. `degraded` is deliberately OUTSIDE the non-terminal
-- set: a degraded operation is visible/actionable but must not wedge the platform against new
-- operator activations (data-model.md §States).

CREATE TABLE IF NOT EXISTS activation_operations (
  operation_id    text PRIMARY KEY,
  idempotency_key text NOT NULL,
  state           text NOT NULL CHECK (state IN
    ('prepared', 'committing', 'reloading', 'rolling_back', 'active', 'rolled_back', 'degraded')),
  actor           text NOT NULL CHECK (actor <> ''),
  target_model    text NOT NULL CHECK (target_model <> ''),
  target_version  text NOT NULL CHECK (target_version <> ''),
  previous_model  text,
  previous_version text,
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  last_error_code text,
  last_error      text,
  evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- a previous VERSION never appears without its model; the converse is allowed because the 022
  -- ActiveServingLLM pointer is name-scoped (each model's @serving alias carries the version), so
  -- a rollback target is a model name whose version may be unknown at capture time
  CHECK (previous_version IS NULL OR previous_model IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_nonterminal_activation
  ON activation_operations ((true))
  WHERE state IN ('prepared', 'committing', 'reloading', 'rolling_back');

CREATE UNIQUE INDEX IF NOT EXISTS uq_activation_idempotency_nonterminal
  ON activation_operations (idempotency_key)
  WHERE state IN ('prepared', 'committing', 'reloading', 'rolling_back');

CREATE INDEX IF NOT EXISTS ix_activation_created
  ON activation_operations (created_at DESC);
