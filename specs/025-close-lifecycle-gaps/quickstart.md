# Quickstart — Validating Close Lifecycle Gaps

Unlike 024, this feature changes behavior, so validation is "the new capability works AND nothing else
regressed." Offline where the logic is web-free; on the RTX 5070 Ti for the GPU-touching legs
(constitution gate zero). Implementation lives in tasks.md.

## Prerequisites

- Repo on the working branch; Python env for the offline suite (stdlib + pytest).
- For US1's load-under-lease leg and any GPU SC: the stack up on the target GPU box (`make up`).
- Tabular (US2) is CPU/off-lease — no GPU needed.

## 1. Nothing regressed (SC-183)

```bash
make lint test spec-check
```

Expected: green, unchanged; no existing test weakened.

## 2. Batch correctness (US1 → SC-175/SC-176)

```bash
pytest -q tests/test_batch_version_assert.py
```

Expected (offline, injected predict_fn + fake admission): a batch requesting version A while B is
"resident" asserts/loads A and scores A — never B — then **restores B** in a `finally` (asserted on both a
successful and a failed batch), and refuses cleanly if a job holds the GPU. ASR batch is rejected at
submission (status quo; a real ASR path is optional net-new).

```bash
# on the RTX 5070 Ti (SC-175):
make up && <launch a batch for a non-resident version>   # scores that version under the single lease,
                                                          # then the prior target is resident again
```

## 3. Tabular full modality (US2 → SC-177/SC-178)

```bash
pytest -q tests/test_tabular_eval.py tests/test_tabular_finetune.py
```

Expected (CPU, web-free/seam-level): the tabular prediction factory + the existing `auc` metric + gate run over `benchmarks/tabular/auc_smoke.jsonl`
(AUC no longer a stub); the tabular fine-tune flow registers a version with its logged metric and cleans
up on failure. End-to-end train→gate→promote→serve runs CPU/off-lease with no new heavy dependency; a
tabular quality window is scorable where labels exist.

## 4. Parked features (US3–US6 → SC-179..SC-182)

```bash
pytest -q tests/test_stream_capture.py          # US4: streamed prediction yields the same log/capture rows
# US3/US5/US6 are console surfaces — verified via their UI tests + a manual console pass:
#   US3 dataset download (no creds in the browser payload)
#   US5 live HPO trial progress (dependency-light)
#   US6 shadow-replay dispatch + advisory verdict from the console
```

## 5. Constitution guardrails

```bash
# no heavy dep crept in; one GPU tenant preserved:
git diff --stat gateway/requirements.txt training/requirements.txt   # expect no heavy adds
```

Expected: no heavy dependency added; tabular holds no GPU lease; the single gated promotion choke-point
is intact; `docs/current-architecture.md` updated if any Snapshot row changed.

## Done signal

Committed-core (US1, US2) green offline + US1 HW leg validated on the box; US3–US6 shipped as independent
slices (or spun into 026+); `make lint test spec-check` green throughout.
