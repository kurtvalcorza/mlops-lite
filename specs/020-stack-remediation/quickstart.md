# Quickstart: 020 Stack Remediation — validation drills

Per-story drills proving the increment end-to-end. Offline steps run anywhere; **[HW]** steps
need the GPU box (and deliberately ride the same session as the pending 018 [HW] sweep).
Contracts: [store-migration](./contracts/store-migration.md), [children-api](./contracts/children-api.md).

## US1 — object-store exit (spike → migrate → cutover → soak → decommission)

Prereq: drive headroom ≥ current object population size ×2 (both stores co-resident).

1. **Spike (gate — FR-202)**: `docker compose up -d garage` beside the running stack; run
   `infra/garage/init.sh`; then per research R2's checklist: MLflow artifact round-trip incl. one
   multi-hundred-MB model (multipart), `platformlib.store` pagination past 1,000 keys, `_missing()`
   404 discrimination, duplicate-PUT write-once behavior, idle RSS at rest — measured as
   `docker stats --no-stream` after ≥5 min idle, both stores, same host (record; SC-130 gate:
   ≤ incumbent). For the suite + smoke-flow leg: **temporarily flip the env seam to the (empty)
   candidate** — the flows self-create their data — run them, then **flip back to the incumbent
   before migration** (this mini-flip is the spike's own rehearsal of the cutover contract).
   Expected: all green. A miss ⇒ switch candidate (SeaweedFS) and repeat this drill.
   *(Terminology: the end-to-end platform "golden/smoke flows" here are unrelated to US2's
   "goldens" — the per-child byte-parity request/response pairs.)*
2. **Migrate**: `python scripts/migrate_store.py --source-endpoint <minio> --dest-endpoint <garage>
   --report /tmp/mig1.json` → `parity: true`; re-run → every bucket `copied: 0` (idempotence,
   SC-127).
3. **Cutover**: flip the three env vars (contract table), `docker compose up -d` + restart host
   venv services; run the golden flows: dataset register → fine-tune (small) → gate → promote →
   infer → drift/quality check → policy tick. Full offline suite passes untouched (SC-128).
4. **Rollback proof** (once, before soak ends): flip the env back, confirm the platform serves
   from the incumbent; `--reverse` mirror carries back any post-cutover writes; flip forward again.
5. **Decommission (operator confirms first — FR-201)**: quiesce writers (stop gateway + agent,
   or at minimum the policy scheduler + prediction/capture logging), then the final forward run
   shows `copied: 0` on every bucket; remove minio+createbuckets per the contract checklist;
   `docker compose config | grep -i minio` → empty; stack restarts clean. Expected end state:
   zero unmaintained components (SC-129).

## US2 — Bento-ectomy (per child; vision, then embed + tabular)

1. **Capture goldens (pre-swap)**: `python scripts/capture_goldens.py --engine vision` (then
   embed/tabular) against the running stack → `tests/goldens/<engine>/`.
2. **Swap one child**: point the adapter's launch at `serving/children/<engine>_service.py`'s
   run script; restart the agent; watch it pass `unavailable → cold → loading → ready`.
3. **Replay gate**: `python scripts/capture_goldens.py --engine <e> --replay` → byte-identical
   status/content-type/body per golden (FR-203). Any diff = revert the launch path (rollback is
   the old child, still on disk until all three pass).
4. **After all three**: remove `serving/bento/` + `bentoml` from the venv; reinstall;
   `pip list | grep -i bento` → empty; suite + goldens green (SC-131). **[HW]** for vision
   (GPU child); embed/tabular drills are CPU and run anywhere with the venv.

## US3 — agent runtime drill **[HW]**

1. Baselines: the 017/018 runbook stream numbers (TTFT, stall threshold, multipart RTT).
2. `AGENT_RUNTIME=stdlib python scripts/agent_stream_drill.py --report` — measures: stream
   TTFT + stalls under concurrent `/health` polling; multipart round-trip; mid-stream client
   disconnect (next request must be clean); preempt-during-stream behavior (409-vs-drain per
   lease semantics). Repeat with `AGENT_RUNTIME=uvicorn`.
3. Append both RuntimeBaselineRecords to `docs/on-hardware-validation-018.md`; verdict per
   FR-205: any stdlib baseline miss ⇒ default flips to uvicorn and the agent suite re-runs on it;
   no miss ⇒ stdlib stays. Either way the record exists (SC-132).
4. Offline (anywhere): the parameterized agent HTTP suite passes on BOTH runtimes while the
   switch exists.

## US4 — GPU-budget portability audit

1. `grep -rn` for VRAM-budget literals outside the single `VRAM_GB` default + docs → none
   (FR-207).
2. With the GPU reader stubbed unreadable and `VRAM_GB=16`: a **15.0 GB**-estimate load is
   admitted, a **15.5 GB** one refused — the static-fallback threshold is 16 × 0.95 =
   **15.2 GB** and moves with the knob (SC-133; offline test).
3. Bring-up checklist present in the README refresh (rides T379): `VRAM_GB`, native builds,
   CUDA-index wheels, `gen_secrets`, renamed-host beacon note.

## Rollback summary

| Story | Rollback | Until |
|---|---|---|
| US1 | env flip back (+ `--reverse` delta mirror) | operator confirms decommission |
| US2 | per-child launch-path revert | `serving/bento/` deleted (after all three gates pass) |
| US3 | `AGENT_RUNTIME=stdlib` | losing runtime deleted next increment |
