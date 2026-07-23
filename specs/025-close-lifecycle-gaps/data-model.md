# Phase 1 Data Model — Close Lifecycle Gaps

This feature is mostly capability/fix over existing data. **No new persisted table is anticipated**; any
genuinely-needed change lands as a NEW numbered `platformlib/migrations/*.sql` (FR-012). The "model" here
is the reused/extended surfaces.

## Reused persisted state (no schema change expected)

| Concern | Existing store | Reuse for |
|---|---|---|
| Predictions / labels / capture index | `gateway` Postgres via `platformlib.store` | Tabular quality (US2) + streamed-prediction capture (US4) reuse the same rows/contract |
| Model versions + logged eval metric | MLflow registry | Tabular register-with-metric (US2) |
| Dataset versions + manifests | content-addressed on Garage | Dataset byte-download (US3) reads existing objects |
| Batch results | content-addressed on Garage (`batch.py`) | Unchanged; US1 only fixes which version is scored |

## New non-persisted surfaces

- **Tabular eval fixture** — `benchmarks/tabular/auc_smoke.jsonl` (committed held-out rows), analogous to
  `benchmarks/vision/shapes_smoke.jsonl`. Not a DB entity.
- **Tabular metric** — AUC promoted from stub to a committed `Metric("auc", HIGHER, auc)` in the existing
  metric interface (`gateway/app/evaluation.py`); pure-Python.
- **HPO progress stream (US5)** — an in-process, ephemeral progress feed (trial index + objective);
  **not persisted** — reconstructable from the running study, no table.

## Batch version-assertion contract (US1)

```
batch_infer(model, registry_version, dataset, ...):
  target = resolve(model, registry_version)          # NEW: explicit target
  under admission lease:
    if a non-preemptable job holds the GPU: REFUSE (clear error)   # never preempt (Principle II)
    ensure target resident (load once for the batch)               # NEW: not "whatever is resident"
  score each record against target                                 # unchanged scoring core
```

Outcome vocabulary: `scored(target)` | `refused(job_holds_gpu)` | `error(unresolvable)`. The scoring core
(`gateway/app/batch.py:run_batch`) is unchanged; only *which version it scores* becomes explicit.

## Tabular as a full modality (US2)

Tabular joins the existing per-modality contract used by vision:
`fine-tune flow → register version (task=tabular, engine tags, logged AUC) → gate on held-out fixture →
promote → serve (existing LightGBM child) → quality window (where a per-request label exists) → breach→retrain`.
No new entity — it fills the produce/eval/monitor columns that were previously stubbed for tabular.
