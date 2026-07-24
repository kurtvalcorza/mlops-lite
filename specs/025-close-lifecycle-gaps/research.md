# Phase 0 Research — Close Lifecycle Gaps

Decisions to confirm during implementation, with rationale and rejected alternatives. Spec-level scope
was fixed in the `/speckit-specify` clarifications (spec.md §Clarifications) — no open NEEDS CLARIFICATION.

## D1 — Batch version-assertion (US1): load-and-assert under the lease, refuse if a job holds the GPU

**Decision**: In `batch_infer.py`, resolve the requested `model`/`registry_version`, and load/assert it
**once per batch** through the agent's admission lease before scoring; if a training/HPO job holds the
GPU, **refuse cleanly** (never preempt). This closes the explicit-`registry_version`-honoring gap while
preserving Principle II. Note: 015 deliberately scoped batch OUT of SC-068 (`015/research.md:70-71` —
batch scoring the resident `@serving` model is correct production behavior); 025 does not overturn that,
it only makes a batch that names a *specific* non-resident version score that version instead of the resident one.

**Rationale**: The current flow scores whatever is resident — a silent wrong-answer. Loading through
admission (not a side channel) keeps the single-tenant invariant; refusing (vs. preempting) respects
"running jobs are never preempted." **Restore-after-batch (Codex):** the batch drives the *same* resident
engine online `/infer` uses (single VRAM lease), so loading the batch target must be paired with a `finally`
that restores the prior desired/resident target (or unloads the temporary one) on both success and failure —
otherwise online traffic keeps hitting the batch's version after the job ends.

**Alternatives considered**: *Preempt to load the batch target* — rejected (violates non-preemptable
jobs). *Assert-only, error if not resident* — weaker but acceptable fallback; the spec allows "load or
refuse," so load-under-lease is preferred, assert-and-refuse is the minimum.

## D2 — ASR batch path (US1): add vs remove

**Decision** (corrected): There is **no** accepted-then-failed bug — `hostagent/jobs.py` validates
submissions against `BATCH_MODALITIES`, which already **excludes** `asr` (`GPU_BATCH_MODALITIES`, which
lists `asr`, is only consulted *post*-validation to protect an active GPU batch's serving holder;
removing `asr` there would change no submission behavior and discard that protection). So this is
**net-new-only**: add `asr` to `BATCH_MODALITIES` + a real ASR path in `batch_infer.py` **if** batched
transcription is wanted; otherwise **no action** — asr is already rejected up front.

**Rationale**: Base the decision on the submission validator (`BATCH_MODALITIES`), not the
lease-protection set (`GPU_BATCH_MODALITIES`).

## D3 — Tabular training library (US2): reuse shipped LightGBM, pure-Python AUC

**Decision**: The tabular fine-tune flow uses the **LightGBM already shipped for tabular serving**;
AUC stays **pure-Python** (already implemented as a metric). Flow mirrors `vision_finetune.py`.

**Rationale**: No new heavy dependency (Principle III); tabular is CPU/off-lease (Principle II).
Mirroring the vision flow inherits the modality contract (dispatch, subprocess isolation,
register-with-metric, failure cleanup).

**Alternatives considered**: sklearn/xgboost — rejected (new heavy dep, footprint).

## D4 — Tabular quality label source (US2)

**Decision**: Wire tabular into quality where a clean per-request ground-truth label exists (tabular
classification has real labels, unlike the earlier embeddings/tabular exclusion note). If a subset has
no clean label, **document the exclusion with rationale** rather than fabricate labels.

**Rationale**: Reuses the existing predictions/labels/window machinery; honest about any gap.

## D5 — Streamed-prediction capture (US4): reuse the fail-open seam

**Decision**: Capture `/infer/stream` predictions via the **existing fail-open capture seam** used by
the non-streamed path, off the response path, keyed by prediction id. The generated `prediction_id` MUST
also be delivered to the client (e.g. an initial metadata SSE event) so the streamed caller can attach the
promised delayed label — `quality.log_prediction` mints the id internally and the label endpoint requires a
caller-supplied id, so an undelivered id makes SC-180 unreachable (FR-356).

**Rationale**: Matches the non-streamed contract exactly; never blocks/alters the stream — save for that one
required prediction-id metadata event — reusing the fire-and-forget discipline already proven for
tracing/quality.

## D6 — HPO progress (US5): in-process stream, no external dashboard

**Decision**: Surface trial progress via an **in-process progress stream** the console reads; no
`optuna-dashboard` service.

**Rationale**: Dependency-light, single-machine (Principle III); optuna-dashboard would add a resident
service against the ~3 GB idle budget.

## D7 — Scope discipline for US3–US6

**Decision**: US3–US6 ship as independent slices in priority order; any that proves larger than a slice
**spins into its own feature (026+)** rather than bloating 025 — honoring Principle VII (no big-bang).

## D8 — On-hardware validation

**Decision**: US1's load-under-lease leg (SC-175) and any GPU-touching SC are validated on the RTX 5070
Ti box (constitution gate zero); they are `[HW]` tasks, marked done only after real validation — not
closable from the offline environment.
