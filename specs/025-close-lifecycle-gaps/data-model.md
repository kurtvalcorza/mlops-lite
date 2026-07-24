# Phase 1 Data Model — Close Lifecycle Gaps

This feature is mostly capability/fix over existing data. **No new persisted table is anticipated**; any
genuinely-needed change lands as a NEW numbered `platformlib/migrations/*.sql` (FR-359). The "model" here
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
    prior = current desired target (generation-stamped)            # NEW: capture before loading
    acquire batch-wide exclusion over the shared engine            # NEW: online /infer queues/refuses; batch's OWN rows bypass via a marker/token
    try:
      ensure target resident (load once for the batch)             # NEW: INSIDE the try — a load/OOM failure still restores
      score each record against target                             # unchanged scoring core (rows carry the batch marker)
    finally:
      desired = re-read latest desired target                      # NEW: a promote may have landed mid-batch (reload deferred)
      restore desired (or unload target); release exclusion        # NEW: restore the CURRENT desired, not the stale snapshot
```

Outcome vocabulary: `scored(target)` | `refused(job_holds_gpu)` | `error(unresolvable)`. The scoring core
(`gateway/app/batch.py:run_batch`) is unchanged; only *which version it scores* becomes explicit. This is
**normative contract, not task prose**: the batch drives the same resident engine online `/infer` uses
(single VRAM lease). (1) The `ensure target resident` load is INSIDE the `try` so a spawn/readiness/OOM
failure that already disturbed the prior engine still hits the restore. (2) The **current desired** target — the
prior one, unless a promote legitimately changed it mid-batch — MUST be resident/converged on every
non-refused path, success OR any raise. The `finally` MUST therefore **re-read the latest desired target**
(a promote can land mid-batch: `models.py:124-142` moves the pointer while `swap.py:170-171` defers the
reload because a GPU batch is active) rather than blindly restore the captured snapshot, or it erases the
newer promotion. (3) A batch-wide exclusion (not just `_gpu_batch_active` eviction-blocking) MUST
queue/refuse online `/infer` for the temporary target's lifetime — while letting the batch's OWN rows through
(they post to the same `/engines/*` paths in separate agent threads, so they need a marker/token or an
agent-internal seam, else the batch deadlocks against itself). Hardware validation (SC-175) MUST assert the
resident identity is the prior target again after both a successful and a **load-failed** batch, and that a
concurrent online `/infer` during the batch never observes the temporary version.

## Tabular as a full modality (US2)

Tabular joins the existing per-modality contract used by vision:
`fine-tune flow → register version (task=tabular, engine tags, logged AUC) → gate on held-out fixture →
promote → serve (existing LightGBM child, **version-aware reload on promote** — a warm child must be invalidated/reloaded, not left on the old booster) → quality window (where a per-request label exists) → breach→retrain`.
No new entity — it fills the produce/eval/monitor columns that were previously stubbed for tabular.
