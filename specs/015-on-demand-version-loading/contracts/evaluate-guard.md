# Contract: `POST /models/{name}/evaluate` guard (FR-143)

The **only** external contract change in 015. `compare`, the promotion gate, and quality are unchanged —
they read logged metrics.

## Request (unchanged)

```json
{ "version": "<str>", "benchmark": "<path?>", "metric": "<name?>" }
```

## Behavior change

The gateway `/evaluate` must never **silently score the resident model** for a different requested version
(the SC-068 mislabel).

| Condition | Response |
|---|---|
| Requested version **is** `@serving` (the resident model is the requested one) | Score it via the serving daemon as today → `200` with the `EvalResult`. |
| Requested version **has a logged metric** (scored at registration, D2) | Return the logged metric (or re-score only the `@serving` case) → `200`. |
| Requested version is **not `@serving`** AND has **no logged metric** | **`409` (or `422`) with a clear error**: e.g. `"<name>@<version> is not the @serving model and has no logged eval metric — promote/serve it to evaluate, or it is scored at registration (015)."` |

## Why

After 015, fine-tuned versions are scored at registration, so the common path is "read the logged metric."
The only gap is an operator asking to evaluate a non-served, never-scored version (e.g. a base/seed model)
— which the gateway cannot score in-process (it is a thin Docker proxy with no torch/GPU). The guard makes
that an explicit error instead of a wrong-model score.

## Acceptance

- `/evaluate` for a non-`@serving` version with no logged metric → clear error, **not** a score (SC-091).
- `/evaluate` for the `@serving` version → scores it (unchanged).
- `/compare` of two scored versions → reads logged metrics, **no model reload** (SC-090).
