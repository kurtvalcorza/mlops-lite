# 018 handoff — continuing after the first landing (PR #27, merged)

**For**: a fresh session (no prior context) picking up the remaining 018 work.
**As of**: 2026-07-03, master `0f9f6f1` (= PR #27 merged: 22 of 38 tasks, Setup + US1 + US2
skeleton + US3 + T380).
**Read order**: `CLAUDE.md` → `plan.md` (this dir) → `tasks.md` (the `[x]` marks are current) →
`quickstart.md` (per-phase drills + rollback). This file adds the session knowledge that is NOT
in those artifacts: verification state, load-bearing design decisions from six review rounds,
and per-task gotchas.

## 1. Where things stand

- **Done**: T343–T357 (platformlib, settings, US1 P0 fixes, hostagent skeleton:
  admission/lifecycle/swap/journal/metrics/HTTP), T366–T371 (policy loop, promotion modes, UI),
  T380 (SC-114 stub-adapter proof).
- **Remaining**: T358–T365 (engine fold-ins → lockfile retirement → [HW] sweep), T372 ([HW]
  loop drill), T373–T377 (US4 relational state; needs T362), T378 (operator decision),
  T379 (docs refresh, after fold-ins).
- **Suite**: `python -m pytest tests/` from the repo root → **310 passed / 30 skipped**
  offline (skips are live-stack guards in `tests/conftest.py`). UI: `cd ui && npx tsc --noEmit`
  → clean. Lint: `ruff check` (pyproject, line-length 100) — the repo has ~900 pre-existing
  findings; the house bar is **zero NEW findings on touched files** (diff against the previous
  commit), not a clean repo.
- **Review history**: 64 findings absorbed — six Codex rounds (11+9+7+6+6+6=45), two
  claude-review CI passes (4), one internal multi-agent review (15 confirmed). Every fix has a
  pinned regression test naming its round (grep `Codex round` / `internal review` in tests/).
  The final CI pass on `a4e0ff7` verified all of them fixed and found nothing new. Codex hit
  its usage quota on 2026-07-03; `@claude` on a PR still triggers the CI reviewer.

## 2. Verify FIRST on a Docker-capable machine

The dev container had no Docker daemon, so one merged change was never executed:

- `docker compose build gateway` — the image now builds from the **repo root** context
  (`docker-compose.yml` → `context: .`, `dockerfile: gateway/Dockerfile`) and COPYs
  `platformlib/` into the image (the compose bind mount was removed). The root `.dockerignore`
  uses the `!gateway` + `!gateway/**` double form (last-match-wins semantics differ across
  builders). If the build fails on `COPY gateway/requirements.txt`, the `.dockerignore`
  re-includes are the first suspect.

Also note: the offline container needed `pip install optuna` (missing HPO test dep); a fresh
environment may too.

## 3. Load-bearing design decisions (do not regress these)

**Admission / swap (hostagent)**
- The swap transaction uses a **reservation** (`Admission.begin_swap / retarget_swap /
  end_swap`), NOT the admission lock held across engine calls. Global lock order is
  **engine `rt.lock` BEFORE `admission.lock`**, everywhere; holding `admission.lock` across
  `ensure_loaded()`/`unload()` deadlocks ABBA against the reaper thread (pinned in
  `tests/test_agent_swap_txn.py`).
- Swap probes the target (`enabled` / `wedged_reason` / `available()`) **before** evicting; a
  post-eviction load failure **retargets** the reservation at the evicted holder and reloads it
  (rollback) — never `end_swap` first, that reopens the snipe window.
- `EngineRuntime._loading`: a drain-timeout hard cut while a load is in flight returns `busy`
  instead of tearing down lock-free (the loader owns the admission slot before `self.child`
  exists). `_teardown` on a non-resident child clears `wedged_reason` and releases the slot
  (a wedged child that later exits must not pin the GPU).
- Legacy-lease interop raises are mapped by exception NAME to the agent's `Held`/`VramExceeded`
  (`hostagent/admission.py`) — the lease module is separately loaded, so `isinstance` won't work.

**Lease (`serving/gpu_lease.py` — all of it retires at T364)**
- State dir `MLOPS_STATE_DIR` (default `~/.mlops-lite`); fixed rendezvous pointer
  (`MLOPS_STATE_POINTER`) + per-dir beacon, both written atomically (tmp + `os.link`); beacon
  divergence **self-heals when no live holder** (reboot/hostname change), refuses otherwise.
- Same-pid records are only "ours" if `pid_start` matches (`_ours()`); a live holder at the
  pre-018 `/tmp/mlops-lite-gpu.lease` blocks fresh claims during mixed-version upgrades
  (`MLOPS_LEGACY_LEASE_PATH` is a test seam; `tests/conftest.py` isolates it).

**Policy scheduler (`gateway/app/scheduler.py`)**
- `quality.try_reserve_retrain()` returns a **token**; fresh reserve→launch failure paths
  release WITH the token; the parked-retry failure release is **deliberately tokenless** (the
  park owns the current stamp via its `note_fn()` keep-alive refresh on every busy retry —
  a token-guarded release there would leak the reservation). Pinned in
  `tests/test_policy_scheduler.py` (`Cooldown.releases`).
- A consumed-but-uncleared park is marked `"landed": True` — `_handle_breach` must NOT
  supersede landed parks (that inherits the far-future retry time and the retrain never fires).
- Watches: poll failures map to `"unknown"` inside `_default_watch`; unknown watches expire
  after `POLICY_WATCH_UNKNOWN_S` (default 3600 s) via `unknown_since`.
- `_default_shadow`: listing failures RAISE (fail-closed — None means "no window" = green);
  per-key read errors skip-and-continue; verdicts count only when their recorded
  `champion.version` equals the current incumbent (`_current_serving_version` seam — patch it
  in offline tests; it lazily imports `app.registry`, and some tests stub `mlflow` in
  `sys.modules`).
- `_default_launch` force-overwrites `output_name = policy.model_name` (the loop gates/promotes
  by policy model + returned version); write-time validation also rejects a mismatch.
- Suggestions: `create_suggestion` is idempotent per (model, version, state);
  `resolve_suggestion` raises `SuggestionConflict` → 409 (not `PolicyError` → 400); accept has
  an already-serving short-circuit and reports `promoted: true` truthfully even when the
  post-promote state write blips.

**Misc**
- `hostagent/main.py` routes on `urlparse(self.path).path`; `/jobs?kind=` filters.
- `AGENT_BIND` defaults `0.0.0.0` deliberately (containers reach :8100 via host-gateway);
  research R3/R6 document this. Env surface added by 018: `AGENT_URL`, `AGENT_BIND`,
  `AGENT_CONTROL_SECRET`, `MLOPS_STATE_DIR`, `POLICY_SCHEDULER_ENABLED`, `POLICY_TICK_S`,
  `POLICY_RETRY_BASE_S`, `POLICY_RETRY_MAX_S`, `POLICY_WATCH_UNKNOWN_S`.
- `platformlib.topology.NON_PREEMPTABLE_KINDS` is the single job-guard definition the agent
  swap consults; `NON_PREEMPTABLE` (tenant form) is the legacy gateway one, gone at T364.

## 4. The remaining work, in order

Sequence per tasks.md: **T358 → T359 → T360 → T361 → T362, then T363 → T364 → T365 [HW]**;
T372 [HW] anytime after; US4 (T373–377) after T362; T378/T379 last.

Per fold-in (T358–T361), the recipe SC-114 proved: one adapter module in `hostagent/adapters/`
(copy the duck-typed interface from `tests/test_agent_adapters.py`'s stub: `engine_id`, `gpu`,
`optional`, `available()`, `estimate_vram()`, `spawn()`, `ready()`) + one `ENGINES` registry row
+ the gateway URL flip + delete the legacy daemon **in the same phase**. Offline tests first;
[HW] smoke per quickstart before merging; rollback = flip the env URL back.

- **T358 LLM**: port `serving/llama/supervisor.py` spawn/ready/forward incl. SSE streaming for
  `/engines/llm/infer/stream`; flip `SERVING_URL`; gateway `serving.py` reads agent health.
- **T359 ASR**: multipart forward; opt-in engine; `unavailable` when the CUDA build is absent;
  keep `build.sh`.
- **T360 Vision**: wrap the BentoML child (R10); strip the in-service lease/unload code from
  `serving/bento/service.py`; gateway `vision.py` drops busy-marker mapping for agent 409s.
- **T361 CPU** (embed/tabular): children with `gpu=False` — no admission by construction.
- **T362 Jobs** (the big one): port the trainer's four launch paths into `hostagent/jobs.py`
  (subprocess-per-run, child pid = VRAM owner via lifecycle `set_child`); `POST /jobs` + legacy
  aliases per contracts/agent-api.md; retire `training/trainer.py` AND the four path-injection
  seams listed in the task (→ `platformlib` imports). US4 depends on this.
- **T363**: gateway swap thins to passthrough; health/metrics read the agent's single health.
- **T364 lockfile retirement**: delete `serving/gpu_lease.py` + the interop shim (`lease=` in
  `Admission`, `LEGACY_TENANT`, the legacy exception mapping) + the legacy-/tmp guard +
  `tests/test_lockfile_interop.py` semantics; `supervise` shrinks to `{agent, ui}`; single
  `AGENT_URL`; free 8090–8095/8099; update `tests/conftest.py` fixtures (incl. the
  `MLOPS_LEGACY_LEASE_PATH` isolation, which becomes dead).
- **T365 / T372 / T377 [HW]**: run the quickstart drills, record in
  `docs/on-hardware-validation-018.md` (create it; 017's runbook is the model).
- **US4 (T373–377)**: contracts/store-schema.md is written; `policy_status`'s O(N) suggestion
  scan and `_default_shadow`'s full-prefix listing are known object-scan hot spots the cutover
  should eliminate.
- **T378**: operator-only (constitution wording). **T379**: README says "through 014" — stale.

## 5. House conventions

- FR/SC/T numbering continues the shared space (next: FR-198, SC-127, T401). 019
  (review-remediation) used FR-188..197 / SC-117..126 / T382..400; FR-187 / SC-116 / T381 were
  left unused (a harmless one-each gap — nothing references them).
- Every fix ships with a regression test whose comment names the finding's source round.
- Offline-testability: injectable seams (`store=`, `check_fn=`, `read_fn=`, `_s3()`…), FakeS3
  in `tests/_quality.py`, importlib standalone loading (keep `gateway/app/swap.py` and
  `quality/batch/shadow` free of top-level relative imports — see the `settings.py` docstring
  for the deliberate env-read exceptions).
- Remote sessions: branch `claude/mlops-lite-architecture-6a7iw2` restarts from latest master
  now that PR #27 is merged (`git checkout -B <branch> origin/master`); one draft PR per
  landing; commit messages follow the `018: <what> — <why>` shape.
