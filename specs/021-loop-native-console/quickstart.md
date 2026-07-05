# Quickstart: 021 Loop-Native Operator Console — validation drills

Browser-driven per-story drills proving the IA rebuild end-to-end. Run the console with
`cd ui && npm run dev` (serves `127.0.0.1:3000`) against a running platform stack. The automated gate
is `npm run lint` + `npm run build` (type-check) — there is no UI unit/e2e harness (research R8).
Contracts: [nav-and-routes](./contracts/nav-and-routes.md), [allowlist-delta](./contracts/allowlist-delta.md).

## US1 — the nav bar IS the loop (P1)

1. Load `127.0.0.1:3000`. Expected: redirects to `/serving`; the nav renders
   `data → training → models → serving → monitoring → retraining ⟲` in order, with connectors + a
   loop-back marker; `health` and the GPU pill are off-axis (right).
2. With a run active and ≥1 open suggestion: without opening a tab, the `training` badge shows an
   active-run indicator and `retraining` shows the open-suggestion count.
3. With a model resident: the GPU pill shows holder + resident model + swap/idle; click it → lands on
   `/serving` lease view.
4. Stop the gateway; reload. Expected: nav still renders and navigates; badges + pill show
   unknown/at-rest; no blank/broken page (SC-142).

## US2 — serving under one lease (P1)

1. With several tasks promoted, open `/serving`. Expected: one panel per promoted task; an untagged
   serving version → read-only "no renderer" placeholder.
2. Submit an LLM prompt. Expected: streamed completion + the resolved registry version + the created
   `prediction_id` + cold-start load time; the `prediction_id` is shown as feeding monitoring, with a
   "label this prediction" hand-off.
3. Exercise vision / tabular / transcribe / embed panels — each returns its result.
4. Launch a batch job over a `dataset@version`; poll it to a result link (all within `/serving`).
5. With a model resident, trigger a preemptive swap. Expected: a confirm dialog names the holder to
   evict; only on confirm does it proceed. Lease view distinguishes lease-tenant (llm/vision/asr/
   training) from off-lease (tabular/embed) engines.

## US3 — monitoring read-side (P2)

1. Open `/monitoring`. Expected: recent **drift** and **quality** histories both render, newest first.
2. Run a drift check (reference vs current `dataset@version`, threshold) → PSI report renders.
3. From `/serving`, use "label this prediction" → lands on the labels panel with `prediction_id`
   prefilled; submit a label. Re-submit the same id → reported as duplicate (not overwritten); submit
   an unknown id → reported cleanly.
4. Enable the one-shot "retrain if this breaches" on a check that breaches. Expected: the retrain spec
   is pre-filled from the breached model (`dataset_version=latest`, modality/output prefilled, knobs
   defaulted) and requires confirmation.
5. Fire a second breach immediately. Expected: `skipped: cooldown` shown as a first-class outcome, not
   an error.

## US4 — autonomous retraining (P2)

1. Open `/retraining`; declare a policy via the form. Submit an invalid one → field-level errors show
   inline and it is not stored. Toggle to the JSON view → same document; save a valid one.
2. Cycle board: each policied model shows last check / next due / pending retrain.
3. Enable auto-promote. Expected: an explicit warning confirm ("the platform will move @serving
   without you"); off by default.
4. Suggestions inbox (filter open/accepted/dismissed): accept a suggestion → promotes through the
   gate. Force a gate-blocked candidate → the suggestion stays open and offers "review & override in
   models" (deep-link); confirm override is NOT available on accept itself.

## US5 — models registry + promote gate (P3)

1. Open `/models`; a model's serving champion is marked; version rows show lineage links back to the
   run and dataset; a seeded version (no `run_id`) is visually distinct.
2. Evaluate a version (default benchmark/metric) → score shown, alias unmoved; open the advanced
   disclosure → override benchmark + metric.
3. Preview → promote a passing candidate → alias moves. Promote a hard-gate-failing candidate →
   alias unmoved, block verdict shown; override requires a confirm dialog capturing a typed reason.

## US6 — data & training entry (P3)

1. Open `/data`; register a version; inspect it → full manifest + download; validate → readiness
   report with gate vs warn dispositions. Click "train on this version" → `/training` opens with
   `dataset@version` prefilled.
2. In `/training`, select each modality → only its knobs + pinned default base show; vision's base is
   read-only/locked; the chain-from-parent field appears only for vision/embeddings.
3. Launch a run while a serving model is resident such that admission refuses → the busy/over-budget
   refusal renders as a distinct first-class outcome. On completion, "view in models" hand-off works.

## US7 — enriched health (P3)

1. Open `/health`. Expected: overall platform liveness + a per-engine probe dot for each serving
   subsystem; with one engine down, its dot is distinctly not-ok.

## Regression gate (all stories)

- `npm run lint` and `npm run build` pass (type-check clean after the rename/relocate).
- The platform's Python backend test suite passes unchanged (SC-141 — 021 touches no backend).
- Grep check: every gateway call in `ui/` resolves to an entry in `gw-allowlist.ts` (no non-listed
  path/method); the allow-list diff equals the additions in the contract (SC-140).
