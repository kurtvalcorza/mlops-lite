# Handoff — 018 US4 T374 (quality/labels store cutover), and where everything stands

_Written 2026-07-04 before a conversation compaction. Delete this file before the final T374 PR._

## 1. Big picture — 018 status

Repo: `github.com/kurtvalcorza/mlops-lite` (PUBLIC), local `C:\Users\Kurt Valcorza\Projects\mlops-lite`.
Spec-Kit driven. Constitution Principle II = **one GPU tenant in VRAM at any instant (non-negotiable)**.

**018 structural work COMPLETE + [HW]-validated + merged** (all on `master`):
- T358–T361 engine fold-ins (llm/asr/vision/embed/tabular → `hostagent/adapters/`).
- T362 jobs fold-in (`hostagent/jobs.py`, `kind="job"` + journal; `training/trainer.py` retired).
- T362.1 FR-176 seams (`platformlib/s3io.py`, `platformlib/gateway_bridge.py`).
- T363 gateway swap → passthrough (`gateway/app/swap.py` deleted; agent orchestrates preempt).
- **T364 lockfile retirement** (#38 `8bcf4fd`): deleted `serving/gpu_lease.py` + shim; single `AGENT_URL`;
  freed ports 8090–8095/8099; `supervise` `{agent,ui}` unconditional restart. Admission (one re-entrant
  lock) is the SOLE GPU authority.
- **T365 [HW] sweep** (#40 `c65bf0f`): SC-106..110 ALL PASS; built `scripts/swap_stress.py`;
  runbook `docs/on-hardware-validation-018.md`. Found+fixed 2 real bugs (scheduler.py imported the
  deleted `trainer_url`; FR-174 Prometheus target broken cross-distro → file_sd + up_all IP inject).
- **T373 US4 store client** (#42 `cee9a2f`): `platformlib/store.py` gained the Postgres `gateway`-DB
  side (connect/bootstrap + the 7-table schema + write-once labels PK + `window()` join);
  `psycopg[binary]==3.3.4`; `infra/postgres/init.sql` mirrors the DDL.

**018 remaining after T374:** T375 (jobs/policies/suggestions → tables; agent `journal.py` writes jobs
rows), T376 (`scripts/backfill_store.py`), T377 [HW] SC-111, T372 [HW] SC-112 (policy loop; runnable,
launches a real vision retrain), T378 constitution v1.5.x wording (operator decision), T379 docs (likely
covered by the **open PR #41** "docs: update README + monitoring", yours, MERGEABLE/CLEAN).

**PARALLEL spec `020-stack-remediation`** landed on master (T401+, FR-198+): object-store exit
(MinIO→Garage/SeaweedFS), Bento-ectomy (native serving children), agent-runtime decision, GPU-budget
portability. Its own spec explicitly says it's **decoupled from US4** ("whichever lands second has fewer
prefixes to care about"). Only real friction: both edit `platformlib/store.py` (US4 the relational side,
020-US1 the S3 side) — don't develop them against that file simultaneously. `CLAUDE.md` now points at
`specs/020-stack-remediation/plan.md`.

## 2. T374 — the current task (IN PROGRESS)

**Branch:** `claude/mlops-lite-018-us4-t374` (from master). **WIP checkpoint commit `1b6af96`** — NOT for
merge; the offline suite is NOT green until the pending items land.

**Kurt's decision (AskUserQuestion):** the **CLEAN CUTOVER** — `window()` fully replaces the object-scan
reads; rework the ~32 FakeS3 tests to inject a store seam. (He rejected the dual-write+object-fallback
option.)

### Pinned data model (do NOT relitigate)
- Relational **rows are the indexed JOIN surface**; the **bulky/variable bodies stay in MinIO objects**:
  the prediction OUTPUT at `predictions.payload_ref` (the `predictions/{pid}.json` object), the recoverable
  captured INPUT at `capture_index.input_ref` (the `inputs/<modality>/...json` object).
- **Labels go FULLY relational** — `labels` table only; write-once = the `labels` PRIMARY KEY (FR-185),
  no in-process lock. This is the headline win.
- **Quality REPORTS + shadow VERDICTS stay objects** (no relational table for them — `results/quality/*`,
  `results/shadow/*` unchanged).
- `window()` (predictions⋈labels, `served_at DESC LIMIT n`) and `replay_window()`
  (capture_index⋈predictions⋈labels, `captured_at DESC LIMIT n`, TTL in WHERE) return only the bounded N
  rows; the caller then GETs ≤N objects for the bodies. That is the SC-111 win (was: list+read every object).

### DONE in commit `1b6af96`
- `platformlib/store.py` read helpers: `replay_window`, `prediction_exists`, `capture_rows`,
  `delete_capture`, `has_captures` (plus `window` from T373). All SQL lives in store.py.
- `gateway/app/quality.py` fully cut over — **compiles + ruff-F/E7 clean**:
  - `_store = platformlib.store` (module-level, **swappable for a fake in tests**); lazy fail-open
    `_conn()` (returns None on outage, bootstraps once); `_dropped(kind)` →
    `gateway_quality_store_dropped_total` counter (FR-164); `reset_store_conn()` test seam.
  - `log_prediction`: writes the OUTPUT object (`payload_ref`) **and** the `predictions` row (fire-and-forget,
    both fail-open with drop-counter). `streamed=(captured_pred is None)`.
  - `attach_label`: relational — `prediction_exists` → "unknown"; `store.attach_label` → `LabelExists`
    → "duplicate". `_label_write_lock` DELETED. **Fails LOUD** (QualityStoreError → 502) on a store outage.
  - `capture_input`: writes the INPUT object (`input_ref`) **and** the `capture_index` row (fail-open).
  - `_prune_inputs`: prunes over `store.capture_rows` (deletes the row via `delete_capture` + the object).
  - `_load_pairs`: `store.window(...)` + bounded `_get_json(payload_ref)` for outputs; skips
    None/streamed; reverses DESC→oldest→newest. Threads `window_n` from `compute_quality`.
  - `_eval`: retired the `from app import evaluation` sys.path fallback (FR-176).

### PENDING (the remaining, larger half — do these next)
1. **`gateway/app/shadow.py`**: cut `resolve_window` over to `quality._store.replay_window` +
   bounded `_get_json(input_ref)` (input body) + `_get_json(payload_ref)` (champion output). Build
   `input_recs`/`predictions`/`labels` then call the existing pure `join_window`. TTL: pass
   `ttl_cutoff=datetime.fromtimestamp(now - ttl_s, tz=utc)` to `replay_window` (TTL now in the WHERE
   clause — no key parse). `has_captured_inputs` → `quality._store.has_captures(conn, modality)`. Retire
   the `try: from . import quality / except ImportError: import quality` fallback (FR-176 — the trainer no
   longer loads shadow standalone; it goes through `platformlib.gateway_bridge`). `resolve_window`'s `s3=`
   param stays for the object GETs; add store access via `quality._conn()`.
2. **`gateway/app/batch.py`**: retire its dual-runtime `sys.path` hack (FR-176). Check what it actually
   does first (`grep -n "sys.path\|from app import" gateway/app/batch.py`).
3. **`tests/_quality.py`**: add a **`FakeStore`** class that mimics the `platformlib.store` **module
   surface** over in-memory dicts: `connect()` (returns a sentinel conn), `bootstrap(conn)`,
   `log_prediction`, `attach_label` (raises `LabelExists` on dup pid), `capture_input`, `window`,
   `replay_window`, `prediction_exists`, `capture_rows`, `delete_capture`, `has_captures`, and a
   `LabelExists`/`StoreError` attr. Tests do `q = load_quality(); q._store = FakeStore(); q.reset_store_conn()`.
   Keep `FakeS3` (still needed for the object bodies: payload_ref/input_ref/quality-report/shadow objects).
4. **Rework the ~10 FakeS3 test files** that exercise the I/O wrappers to also inject the FakeStore:
   `test_quality.py`, `test_shadow_window.py`, `test_shadow_verdict.py`, `test_shadow_insufficient.py`,
   `test_capture_policy.py`, and any others surfaced by
   `grep -rlE "log_prediction|attach_label|_load_pairs|resolve_window|compute_quality|capture_input" tests/`.
   The pure-function tests (score_window/evaluate_quality/join_window/build_verdict/is_breach/inputs_to_prune)
   need NO change — they take injected records.
5. **`tests/test_label_write_once.py`** (NEW, named by the task): concurrent duplicate label submissions →
   exactly one stored, the rest "duplicate" (via the FakeStore's PK, and/or a live-DB variant that skips
   offline like `test_store_client.py`'s live tests). 100-trial style per quickstart §US4.
6. Run the full offline suite (`~/mlops-train/bin/python -m pytest tests/ -q`) green; ruff zero-new on
   touched lines. Then T377 is the [HW] SC-111 (10k window <5s + concurrent-label + gateway+agent restart).

### Callers to double-check aren't broken by the cutover
- `gateway/app/routers/monitor.py` (calls `quality.attach_label`, `compute_quality`, `latest_quality_reports`).
- `gateway/app/scheduler.py` (policy loop calls `compute_quality`).
- `data/submit_labels.py` (POSTs `/monitor/labels`).
- `latest_quality_reports` + `compute_quality`'s report persist still use S3 (quality reports stay objects) — unchanged.

## 3. Environment + gotchas (critical for a fresh context)

- **Shells:** Bash tool = Git Bash. Run daemons/tests/psql-less checks in **WSL Ubuntu**:
  `MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu bash -lc 'cd "/mnt/c/Users/Kurt Valcorza/Projects/mlops-lite" && ...'`.
  Use `$HOME/mlops-train/bin/python` (the training venv — has torch/psycopg/optuna/ruff). PowerShell tool
  for `up_all.ps1` / `docker compose`.
- **Quote-layer gotcha:** inline `wsl bash -lc '... awk "…\$2…" ...'` gets MANGLED through
  PowerShell→GitBash→wsl. **Write a Python/script file instead** (did this for the sweep drills).
- **Filesystems:** Git Bash `/tmp` ≠ WSL `/tmp` (separate). `.env` has CRLF — read with
  `awk -F= '/^KEY=/{print $2}' | tr -d '"\r'` or read it in Python.
- **Store DSN (US4):** gateway container → `postgres:5432/gateway` (compose sets `GATEWAY_DB_URL`);
  native WSL host/tests → `127.0.0.1:55432/gateway`. `POSTGRES_PASSWORD` in `.env`. The `gateway` DB
  exists; `store.bootstrap()` creates the 7 tables idempotently (already applied on the live volume).
- **Live stack:** currently UP (gateway/mlflow/prometheus containers + agent `{agent,ui}`,
  `all_healthy=True`). A standalone `docker compose up gateway` (outside `up_all`) LOSES the injected
  `AGENT_URL` → falls back to `host.docker.internal:8100` (cross-distro unreachable); always set
  `$env:AGENT_URL` first or redeploy via `up_all.ps1`.
- **ruff:** line-length 100; house rule = **zero NEW findings on touched lines** (repo has ~900
  pre-existing E501). New files must be fully clean.
- **Dual-bot review loop:** tag `@claude` + `@codex` in the PR body; fix all findings with regression
  tests; loop until clean; merge. Codex is HARD RATE-LIMITED currently → **merge on @claude's clean
  verdict**. `claude-review` GitHub Action runs `display_report:false` + `classify_inline_comments:true`,
  so a CLEAN pass posts NOTHING (check-pass + 0 inline comments = clean).
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Never put a model
  identifier in commits/PR titles/code. Secret-scan every diff (public repo).
- **Kurt prefers** AskUserQuestion multiple-choice (recommended pick first) for real decisions.

## 4. Immediate next action
Resume T374 from checkpoint `1b6af96`: do `shadow.py` + `batch.py` (§2 items 1–2), then the `FakeStore`
harness + test rework (items 3–5), then green the suite. All design decisions are already made above.
