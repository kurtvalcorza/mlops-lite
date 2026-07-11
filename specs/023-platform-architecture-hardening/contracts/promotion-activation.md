# Contract: Recoverable Registry-Driven LLM Activation

This contract complements 022. 022 owns LLM resolution, registry descriptors, console selection,
and the rule that operator promotion is go-live. 023 defines how that multi-system action survives
partial failure.

## Authorities

- **Gate verdict and model metadata**: MLflow registry.
- **Desired platform-wide LLM**: `ActiveServingLLM` pointer in Postgres.
- **Operation/recovery state**: `ActivationOperation` in Postgres.
- **Actual resident model/version**: host agent only.
- **Prediction identity**: copied from agent-reported actual identity for that request.

No authority may substitute for another during an incomplete activation.

## Prior art — build on the merged 022 offline slice (PR #65 `1008dcc`)

This contract post-dates the review's `42f8c6e` baseline; 022's offline slice has since merged and
already provides the single-shot primitives this contract coordinates. 023 wraps them in the durable
operation — it MUST NOT introduce a parallel rollback:

- **Probe before eviction** is `hostagent/swap.py`'s `TargetUnresolvable` (an `available()` check
  before any unload). The reload route tags it `{"unresolvable": true}`.
- **Restore-previous / degraded** is `registry.restore_serving_llm(prior)`; a failed restore already
  surfaces `{rolled_back: false, pointer_error}` — the degraded outcome this contract names.
- **Desired vs. resident** identity is already agent-reported; prediction logging uses resident.

What 022 lacks and 023 adds: the durable `ActivationOperation` record, per-platform serialization,
the idempotency key, and startup/periodic reconciliation. Implementations extend `restore_serving_llm`
/ the `unresolvable` signal into these states rather than re-deriving them.

## Submit contract

An operator promotion supplies target model/version, preemption confirmation when required, and an
idempotency key. The gateway:

1. authenticates the operator and enforces 022's operator-only live-switch rule;
2. runs the existing promotion gate;
3. resolves/validates full-model or base+adapter artifacts locally;
4. reads current desired and resident identities;
5. refuses/defer when a non-preemptable job holds admission;
6. creates or returns the durable operation.

The same idempotency key with a different target is `409`. With the same target it returns the
existing operation and may trigger reconciliation, never a duplicate reload.

## State transition contract

| State | Required durable evidence | Allowed next |
|---|---|---|
| `prepared` | validated target + previous identities | `committing`, `rolling_back`, `degraded` |
| `committing` | desired pointer/alias step attempts | `reloading`, `rolling_back`, `degraded` |
| `reloading` | idempotent agent command issued | `active`, `rolling_back`, `degraded` |
| `rolling_back` | rollback attempts/evidence | `rolled_back`, `degraded` |
| `active` | alias + pointer + resident all target | terminal |
| `rolled_back` | desired/resident restored/verified | terminal |
| `degraded` | exact mismatch and safe next actions | retry/reconcile or explicit operator rollback |

Each transition is compare-and-set from the expected prior state. Per-platform advisory/application
locking prevents concurrent non-terminal operations.

## Agent reload contract

- Request includes `operation_id`, target resolved identity, and explicit preemption confirmation.
- Agent stores/reports the last completed/in-progress reload operation sufficiently for idempotent
  retry during its process lifetime; durable cross-process truth stays in Postgres.
- Same operation + target returns current result/no-op.
- Same operation + different target is rejected.
- Artifact/target probe happens before eviction.
- A job holder is never preempted.
- Same-engine model switch force-reloads; cross-engine switch uses existing transactional swap.
- Success is not accepted until agent health reports exact target model/version resident.

## Failure and reconciliation

On any ambiguous timeout or restart, reconciliation reads all authorities rather than inferring
success from the last call. It may safely:

- advance to `active` if alias, pointer, and resident target already match;
- reissue an idempotent alias/pointer/reload step;
- restore previous pointer/alias and resident model;
- mark `degraded` if neither target nor previous state can be verified safely.

Reconciliation runs at gateway startup and periodically while non-terminal/degraded retryable
operations exist. A degraded operation is visible in API/UI and alerts. Operators can retry or
request rollback but cannot force the `active` label without resident verification.

## Read contract

Serving status exposes, additively:

```json
{
  "desired": {"model_name": "...", "version": "..."},
  "resident": {"model_name": "...", "version": "..."},
  "activation": {"operation_id": "...", "state": "reloading|active|degraded", "last_error": null},
  "consistent": false
}
```

No secret, filesystem path, raw registry credential, or unrestricted exception text is returned.

## Automation boundary

An automated policy may train, register, evaluate, and suggest/promote metadata according to the
existing policy contract, but it does not initiate live text-generation activation in 023. Only an
authenticated operator action can create the activation operation.
