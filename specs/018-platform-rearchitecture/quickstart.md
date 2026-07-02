# Quickstart: validating 018 Platform Re-Architecture

Per-phase validation guide. Offline checks run anywhere; **[HW]** items need the target
machine (Gate Zero: `nvidia-smi` in WSL). Contracts: [contracts/](contracts/) · entities:
[data-model.md](data-model.md).

## Every merged phase (FR-177 / SC-113)

```bash
python -m pytest tests/ -x -q        # full suite green (skip-guards handle absent daemons)
```

Default-path parity: `/infer`, `/vision/classify`, `/transcribe`, `/embed`, `/predict` without
`preempt` behave byte-for-byte as 017 (existing modality tests are the oracle).

## US1 groundwork (offline)

```bash
python -m pytest tests/test_swap_orchestration.py tests/test_no_preempt_training.py \
  tests/test_drift_loop.py tests/test_quality_breach.py -q
```

- Trainer unreachable + `preempt=true` ⇒ 409 (fail-closed), never an eviction.
- Two concurrent breach checks ⇒ one launch (reserve-before-launch, both signals).
- Log burst ⇒ completions + dropped-counter account for every message.
- >1000 seeded report keys ⇒ complete listing.
- Two daemons pointed at different state dirs ⇒ loud startup failure (FR-166 beacon check).

## Agent skeleton + fold-ins (US2)

Offline, per phase: `test_agent_admission.py` (single slot, no TOCTOU under thread hammer),
`test_agent_lifecycle.py` (fake engine: load→ready→drain→idle→reap; `unavailable` reason
surfaces), `test_agent_swap_txn.py` (contending acquirer never wins between evict and load),
`test_agent_journal.py` (kill −9 mid-job → replay yields `interrupted` + alert),
`test_lockfile_interop.py` (agent tenant vs legacy lockfile tenant: mutual exclusion both ways).

**[HW] per fold-in**: the folded engine serves via
`http://127.0.0.1:8100/engines/<id>/…`; its legacy daemon is gone from `supervise` status;
`nvidia-smi` shows one tenant during a cross-engine contention burst.

**[HW] at completion** (SC-106..110):

```bash
make up-all                          # then:
pgrep -fc 'hostagent|next-server'    # == 2 resident native processes (SC-106)
python -m pytest tests/test_infer_panels.py tests/test_serving.py -q   # five modalities
# SC-107: compare cold/warm latencies vs docs/on-hardware-validation-015-016-017.md baselines
# SC-108: scripts/swap_stress.py — ≥100 preempt cycles, assert 1 tenant max, 0 sniped swaps
# SC-109: kill -9 the agent mid-batch → journal shows interrupted; VRAM at baseline; history intact
# SC-110: docker compose stop gateway → Prometheus target 'hostagent' still up; fork-watch
#         (execsnoop/strace) shows 0 nvidia-smi forks during 60s of UI polling
```

## Policies (US3)

Offline: `test_policy_crud.py` (validation rejects unknown modality/interval<60/empty
monitors), `test_policy_scheduler.py` (fake clock: ticks fire; breach → correct-modality
launch with `latest` dataset; busy ⇒ queue-of-one + backoff; supersede),
`test_promotion_modes.py` (manual = no-op; suggest creates open suggestion; auto-on-green
promotes through the gate and writes audit; gate-blocked auto falls back to suggest).

**[HW] loop drill (SC-112)**: declare a vision policy (`suggest`, 60s interval) on the UI →
inject a quality breach (mislabeled submissions via `data/submit_labels.py`) → within one
interval: vision retrain on latest dataset launches → registers + scores → suggestion visible
on Models page with gate + shadow verdicts. Zero manual invocations in between.

## Durable state (US4)

Offline: `test_store_client.py` (bootstrap idempotent twice; window query shape),
`test_label_write_once.py` (two concurrent writers, one winner, 100 trials),
`test_backfill.py` (objects → rows, idempotent re-run).

**[HW] (SC-111)**: seed 10k predictions+labels → `POST /monitor/quality/check` completes <5s;
restart gateway+agent → `GET /runs`, `/policies/*/status` history intact.

## Rollback per phase

Each fold-in is reverted by flipping the engine's `*_URL` env back to its legacy daemon and
re-enabling it in `SUPERVISE_DAEMONS` — no code revert required until the daemon's deletion
phase (which is the same PR that flips the default; revert the PR to roll back).
